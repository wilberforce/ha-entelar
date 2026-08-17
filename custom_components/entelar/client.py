"""HTTP client for the Entelar / Univers EMS portal.

Same API surface as the standalone `entelar/api.py` script in the
Solar-Project repo, with two changes:

  - `die()` -> raise EntelarAPIError (no sys.exit, this is a library now)
  - no dependency on the `.session` module
"""
from __future__ import annotations

from typing import Any

import requests

from .ca_bundle import get_ca_bundle_path
from .errors import EntelarAPIError

PORTAL_ORIGIN = "https://app.entelarenergy-emsportal.com"

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36 Edg/148.0.0.0"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-NZ,en;q=0.9",
    "Origin": PORTAL_ORIGIN,
    "Referer": PORTAL_ORIGIN + "/hossain-fe/index.html",
    "locale": "en-US",
}


def call(session: dict, path: str, body: Any, *, timeout: int = 30,
         extra_headers: dict | None = None,
         params: dict | None = None) -> dict:
    """POST a JSON body to the portal. Raises EntelarAPIError on error codes."""
    url = session["apiBase"].rstrip("/") + path
    headers = {
        **BROWSER_HEADERS,
        "Authorization": "Bearer " + session["token"],
        "Content-Type": "application/json; charset=UTF-8",
    }
    if extra_headers:
        headers.update(extra_headers)
    r = requests.post(url, headers=headers, json=body, params=params,
                      timeout=timeout, verify=get_ca_bundle_path())
    r.raise_for_status()
    j = r.json()
    code = j.get("code")
    if code == 88201:
        raise EntelarAPIError(path, code,
                              "Token rejected (88201 'Please login first')")
    if code not in (0, None):
        raise EntelarAPIError(path, code, str(j))
    return j
