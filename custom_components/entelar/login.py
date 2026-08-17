"""Login flow for the Entelar / Univers EnOS portal.

Three calls per login:
  1. GET  /hossain-bff/framework/v1.0/user/public-key  -> RSA-2048 pubkey
  2. POST /hossain-bff/framework/v1.0/user/login       -> accessToken + orgs[]
  3. POST /hossain-bff/framework/v1.0/user/set-session -> bind session to org

Token expires in 3600 s (1 hour).

Password is encrypted with RSA-OAEP (SHA-256 + MGF1-SHA-256) on the client
side -- the plaintext password never crosses the wire.
"""
from __future__ import annotations

import base64
import time

import requests
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.serialization import load_der_public_key

from .ca_bundle import get_ca_bundle_path
from .client import BROWSER_HEADERS
from .errors import EntelarLoginError

PUBKEY_PATH = "/hossain-bff/framework/v1.0/user/public-key"
LOGIN_PATH = "/hossain-bff/framework/v1.0/user/login"
SET_SESSION_PATH = "/hossain-bff/framework/v1.0/user/set-session"

TOKEN_LIFETIME_S = 3600


def _headers(token=None):
    h = {**BROWSER_HEADERS, "Content-Type": "application/json; charset=UTF-8"}
    if token:
        h["Authorization"] = "Bearer " + token
    return h


def fetch_public_key(api_base: str):
    """Fetch the portal RSA public key. No auth required."""
    r = requests.get(api_base.rstrip("/") + PUBKEY_PATH,
                     headers={**BROWSER_HEADERS, "Accept": "application/json"},
                     timeout=15, verify=get_ca_bundle_path())
    r.raise_for_status()
    j = r.json()
    if j.get("code") != 0:
        raise EntelarLoginError(f"public-key fetch failed: {j}")
    pem_b64 = j["data"]["publicKey"]
    der = base64.b64decode(pem_b64)
    return load_der_public_key(der)


def encrypt_password(pubkey, password: str) -> str:
    """RSA-encrypt with OAEP (SHA-256 + MGF1-SHA-256), return base64."""
    ciphertext = pubkey.encrypt(
        password.encode("utf-8"),
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    return base64.b64encode(ciphertext).decode("ascii")


def login(api_base: str, account: str, password: str,
          prefer_org_id: str | None = None) -> dict:
    """Run the full login flow. Returns session dict.

    Raises EntelarLoginError on any portal-side rejection.
    """
    api_base = api_base.rstrip("/")
    pubkey = fetch_public_key(api_base)
    encrypted = encrypt_password(pubkey, password)

    r = requests.post(
        api_base + LOGIN_PATH,
        headers=_headers(),
        json={"strategy": "RSA 2048", "account": account, "password": encrypted},
        timeout=20, verify=get_ca_bundle_path(),
    )
    r.raise_for_status()
    j = r.json()
    if j.get("code") != 0:
        raise EntelarLoginError(f"login failed: {j}")
    data = j["data"]
    token = data["accessToken"]
    orgs = data.get("organizations") or []
    user = data.get("user") or {}

    if prefer_org_id and any(o["id"] == prefer_org_id for o in orgs):
        org_id = prefer_org_id
    elif orgs:
        org_id = orgs[0]["id"]
    else:
        raise EntelarLoginError("login succeeded but no organizations returned")

    r = requests.post(
        api_base + SET_SESSION_PATH,
        headers=_headers(token),
        json={"orgId": org_id},
        timeout=15, verify=get_ca_bundle_path(),
    )
    r.raise_for_status()
    j = r.json()
    if j.get("code") != 0:
        raise EntelarLoginError(f"set-session failed: {j}")

    now = int(time.time())
    return {
        "apiBase":    api_base,
        "token":      token,
        "orgId":      org_id,
        "userId":     user.get("id"),
        "userName":   user.get("name"),
        "expiresAt":  now + TOKEN_LIFETIME_S,
        "loggedInAt": now,
    }


def is_expired(session: dict, buffer_s: int = 60) -> bool:
    """True if the token has expired or is within `buffer_s` of expiring."""
    expires_at = session.get("expiresAt")
    if not expires_at:
        return True
    return int(time.time()) + buffer_s >= int(expires_at)
