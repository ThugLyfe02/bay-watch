"""Ephemeral eBay OAuth token minting and refresh. Tokens are never persisted here."""
from __future__ import annotations

import base64
from threading import Lock
import json
import os
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .errors import EbayError


class EbayTokenProvider:
    APP_SCOPE = "https://api.ebay.com/oauth/api_scope"

    def __init__(self, *, env: str, application_token: str = "", user_token: str = "",
                 client_id: str | None = None, client_secret: str | None = None,
                 user_refresh_token: str | None = None):
        self.env = env
        self.root = "https://api.sandbox.ebay.com" if env == "sandbox" else "https://api.ebay.com"
        self.client_id = client_id if client_id is not None else os.getenv("EBAY_CLIENT_ID", "")
        self.client_secret = client_secret if client_secret is not None else os.getenv("EBAY_CLIENT_SECRET", "")
        self.static_application_token = application_token or os.getenv("EBAY_APPLICATION_ACCESS_TOKEN", "")
        self.static_user_token = user_token or os.getenv("EBAY_USER_ACCESS_TOKEN", "")
        self.user_refresh_token = (user_refresh_token if user_refresh_token is not None
                                   else os.getenv("EBAY_USER_REFRESH_TOKEN", ""))
        self._tokens: dict[str, tuple[str, float]] = {}
        self._lock = Lock()

    @property
    def can_mint_application_token(self) -> bool:
        return bool(self.client_id and self.client_secret)

    @property
    def can_refresh_user_token(self) -> bool:
        return bool(self.client_id and self.client_secret and self.user_refresh_token)

    def get_application_token(self) -> str:
        if self.static_application_token:
            return self.static_application_token
        if self.can_mint_application_token:
            return self._get_or_mint("application", "client_credentials", scope=self.APP_SCOPE)
        raise EbayError("Browse OAuth credentials are missing; configure client ID/secret or an application token")

    def get_user_token(self) -> str:
        if self.static_user_token:
            return self.static_user_token
        if self.can_refresh_user_token:
            # Omitting scope keeps the exact scopes granted during the user's consent flow.
            return self._get_or_mint("user", "refresh_token", refresh_token=self.user_refresh_token)
        raise EbayError("Offer OAuth credentials are missing; configure a user refresh token or user access token")

    def _get_or_mint(self, key: str, grant_type: str, **fields) -> str:
        with self._lock:
            cached = self._tokens.get(key)
            if cached and cached[1] > time.monotonic() + 60:
                return cached[0]
            form = {"grant_type": grant_type, **fields}
            payload = urlencode(form).encode("ascii")
            credentials = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode("ascii")
            request = Request(self.root + "/identity/v1/oauth2/token", data=payload,
                              headers={"Authorization": "Basic " + credentials,
                                       "Content-Type": "application/x-www-form-urlencoded",
                                       "Accept": "application/json"}, method="POST")
            try:
                with urlopen(request, timeout=8) as response:
                    result = json.loads(response.read())
            except HTTPError as exc:
                # OAuth error bodies may echo secrets; never include them in logs or audit records.
                raise EbayError(f"eBay OAuth token request failed (HTTP {exc.code})", status_code=exc.code) from exc
            except (URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
                raise EbayError("eBay OAuth token request failed; check network and app configuration") from exc
            token = result.get("access_token") if isinstance(result, dict) else None
            try:
                lifetime = int(result.get("expires_in", 0)) if isinstance(result, dict) else 0
            except (TypeError, ValueError):
                lifetime = 0
            if not isinstance(token, str) or not token or lifetime <= 0:
                raise EbayError("eBay OAuth response did not contain a valid access token lifetime")
            self._tokens[key] = (token, time.monotonic() + lifetime)
            return token
