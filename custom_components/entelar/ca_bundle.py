"""CA bundle helper for Entelar's TLS chain.

The Entelar/Univers portal serves only the leaf TLS certificate; the
intermediate (Sectigo Public Server Authentication CA DV R36) is published
via the certificate's AIA extension, but Python's `ssl` module doesn't follow
AIA automatically -- so validation fails with CERTIFICATE_VERIFY_FAILED even
though the intermediate chains to a root already in the trust store.

We fix this by shipping the Sectigo intermediate PEM alongside this module
(`sectigo_intermediate.pem`) and building a combined bundle (certifi's roots +
the intermediate) once per process, cached in the OS temp dir. No network
access at runtime.

Refreshing the shipped intermediate (only needed if Univers rotates their TLS
cert to one signed by a different intermediate): download the intermediate named
in the leaf's AIA extension from the CA's distribution point, then
    openssl x509 -inform DER -in new.crt -out sectigo_intermediate.pem
and verify:
    openssl x509 -in sectigo_intermediate.pem -noout -subject -fingerprint -sha256
The bundled cert:
    subject = CN=Sectigo Public Server Authentication CA DV R36
    SHA-256 = 8C:54:C3:34:B6:6B:A4:E4:26:77:2A:F4:A3:F9:13:6C:
              19:A1:AE:C7:29:FD:B2:8C:53:5C:07:A5:A4:EF:22:E0
"""
from __future__ import annotations

import logging
import os
import tempfile
import threading

import certifi

_LOGGER = logging.getLogger(__name__)

# Shipped Sectigo intermediate that signs Entelar's leaf certificate.
_INTERMEDIATE_PEM = os.path.join(
    os.path.dirname(__file__), "sectigo_intermediate.pem"
)

_build_lock = threading.Lock()
# Path to the combined bundle for this process; built lazily on first use.
_cached_path: str | None = None


def _build_bundle(out_path: str) -> None:
    """Concatenate certifi's roots + the shipped Sectigo intermediate."""
    _LOGGER.info(
        "Building Entelar CA bundle (certifi roots + Sectigo intermediate)"
    )
    with open(certifi.where(), "r", encoding="ascii") as f:
        certifi_pem = f.read()
    with open(_INTERMEDIATE_PEM, "r", encoding="ascii") as f:
        intermediate_pem = f.read()
    with open(out_path, "w", encoding="ascii") as f:
        f.write(certifi_pem)
        if not certifi_pem.endswith("\n"):
            f.write("\n")
        f.write(intermediate_pem)


def get_ca_bundle_path() -> str:
    """Return the path to a combined CA bundle, building it on first call.

    Rebuilt once per process (from the currently shipped intermediate), so an
    integration update that ships a new cert takes effect on the next restart
    with no stale-cache surprises.
    """
    global _cached_path
    if _cached_path and os.path.exists(_cached_path):
        return _cached_path
    with _build_lock:
        if _cached_path and os.path.exists(_cached_path):
            return _cached_path
        fd, path = tempfile.mkstemp(prefix="entelar-ca-", suffix=".pem")
        os.close(fd)
        _build_bundle(path)
        _cached_path = path
        return _cached_path
