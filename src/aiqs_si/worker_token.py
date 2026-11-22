"""Self-service worker access tokens via the client-credentials grant.

The dedicated worker host holds the si-device-worker client credential in
its own credential environment and fetches short-lived access tokens
itself, refreshing before expiry. No person carries a token: issuance and
renewal happen on the host that uses them. A static token supplied through
AIQS_SI_WORKER_TOKEN remains available as an explicit trial override in
worker_remote; this provider is the standing method.

Errors carry fixed messages only: the client secret, token values and
token-endpoint response bodies never appear in exceptions or logs.
"""
import threading
import time

import httpx

DEFAULT_CLIENT_ID = "si-device-worker"
DEFAULT_SCOPE = "aiqs:worker"
REFRESH_MARGIN_SEC = 60


def token_url_for_origin(origin):
    """Derive the realm token endpoint from the service origin."""
    return origin.rstrip("/") + "/realms/si/protocol/openid-connect/token"


class ClientCredentialsTokenProvider:
    """Callable returning a valid worker access token, fetching as needed."""

    def __init__(self, token_url, client_secret, *, client_id=DEFAULT_CLIENT_ID,
                 scope=DEFAULT_SCOPE, client=None, clock=time.monotonic,
                 wall_clock=time.time, refresh_margin=REFRESH_MARGIN_SEC,
                 on_event=None):
        if not token_url.startswith("https://"):
            raise ValueError("Token endpoint must be an HTTPS URL")
        if not client_secret:
            raise ValueError("A worker client secret is required")
        self._token_url = token_url
        self._client_id = client_id
        self._client_secret = client_secret
        self._scope = scope
        self._client = client or httpx.Client(timeout=15, follow_redirects=False,
                                              trust_env=False)
        self._clock = clock
        self._wall_clock = wall_clock
        self._refresh_margin = refresh_margin
        self._lock = threading.Lock()
        self._token = None
        self._expires_at = 0.0
        self._on_event = on_event

    def set_on_event(self, callback):
        """Attach (or replace) the lifecycle observer after construction."""
        self._on_event = callback

    def _notify(self, event):
        if self._on_event is not None:
            try:
                self._on_event(event)
            except Exception:
                pass  # Observation never alters token handling.

    def __call__(self):
        with self._lock:
            if self._token is not None and self._clock() < self._expires_at - self._refresh_margin:
                return self._token
            self._fetch()
            return self._token

    def invalidate(self):
        """Drop the cached token so the next call fetches a fresh one."""
        with self._lock:
            self._token = None
            self._expires_at = 0.0

    def _fetch(self):
        try:
            response = self._client.post(
                self._token_url,
                data={"grant_type": "client_credentials",
                      "client_id": self._client_id,
                      "client_secret": self._client_secret,
                      "scope": self._scope},
                follow_redirects=False)
        except httpx.TransportError:
            self._notify("token_fetch_failed")
            raise ConnectionError("Worker token endpoint unavailable") from None
        if response.status_code != 200:
            self._notify("token_fetch_failed")
            raise ConnectionError("Worker token request rejected")
        try:
            payload = response.json()
            token = payload["access_token"]
            expires_in = float(payload["expires_in"])
        except (ValueError, KeyError, TypeError):
            self._notify("token_fetch_failed")
            raise ConnectionError("Worker token response malformed") from None
        if not isinstance(token, str) or not token or expires_in <= 0:
            self._notify("token_fetch_failed")
            raise ConnectionError("Worker token response malformed")
        refreshed = self._token is not None
        self._token = token
        self._expires_at = self._clock() + expires_in
        self._issued_wall = self._wall_clock()
        self._notify("token_refreshed" if refreshed else "token_fetched")
