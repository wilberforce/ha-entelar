"""Exceptions raised by the Entelar API client.

These bubble up from login/snapshot calls and the coordinator translates
them into HA error states (ConfigEntryAuthFailed, UpdateFailed, etc.).
"""


class EntelarError(Exception):
    """Base class for all Entelar/Univers portal errors."""


class EntelarLoginError(EntelarError):
    """Login flow failed (bad credentials, RSA decrypt failure, etc.)."""


class EntelarAPIError(EntelarError):
    """A non-login API call returned an error code."""

    def __init__(self, path: str, code, message: str):
        self.path = path
        self.code = code
        self.message = message
        super().__init__(f"{path}: code={code} {message}")
