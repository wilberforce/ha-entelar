"""CA bundle helper for Entelar's TLS chain.

The Entelar/Univers portal serves only the leaf TLS certificate; the
intermediate (Sectigo Public Server Authentication CA DV R36) is published
via the AIA extension but Python's `ssl` module doesn't follow it
automatically. The result is `CERTIFICATE_VERIFY_FAILED` on every API call
even though Genesis's TLS chain validates fine (because Genesis serves the
full chain).

This module bootstraps a combined bundle (certifi's roots + the Sectigo
intermediate) at first use, caches it under /tmp, and exposes a path other
modules pass as `verify=` to `requests`.

The intermediate is fetched over **plain HTTP** by design -- the CA's
distribution URL serves DER-encoded certs unencrypted because the cert IS
the trust anchor; there's nothing to validate against if we used HTTPS.
"""
from __future__ import annotations

import logging
import os
import tempfile
import threading
import urllib.request

import certifi
from cryptography import x509
from cryptography.hazmat.primitives.serialization import Encoding

_LOGGER = logging.getLogger(__name__)

# AIA-published location for the Sectigo intermediate that signs Entelar's leaf.
# If Univers ever changes their TLS cert this URL will change too; rebuild the
# cache by deleting the cached file.
SECTIGO_AIA_URL = (
    "http://crt.sectigo.com/SectigoPublicServerAuthenticationCADVR36.crt"
)

# Cache the combined bundle in the OS temp dir so we don't rebuild on every
# refresh. tempfile.gettempdir() is cross-platform (HA OS, Docker, Windows,
# venv) unlike a hardcoded /tmp.
_CACHE_PATH = os.path.join(tempfile.gettempdir(), "entelar-ca-bundle.pem")

_build_lock = threading.Lock()


def _build_bundle(out_path: str) -> None:
    """Download the Sectigo intermediate (DER) and concat with certifi roots."""
    _LOGGER.info("Bootstrapping Entelar CA bundle (Sectigo intermediate + certifi)")
    with urllib.request.urlopen(SECTIGO_AIA_URL, timeout=20) as r:
        der = r.read()
    cert = x509.load_der_x509_certificate(der)
    intermediate_pem = cert.public_bytes(Encoding.PEM).decode("ascii")
    with open(certifi.where(), "r", encoding="ascii") as f:
        certifi_pem = f.read()
    with open(out_path, "w", encoding="ascii") as f:
        f.write(certifi_pem)
        if not certifi_pem.endswith("\n"):
            f.write("\n")
        f.write(intermediate_pem)


def get_ca_bundle_path() -> str:
    """Return path to a combined CA bundle. Bootstraps on first call."""
    if not os.path.exists(_CACHE_PATH):
        with _build_lock:
            if not os.path.exists(_CACHE_PATH):
                _build_bundle(_CACHE_PATH)
    return _CACHE_PATH
