"""Bound HTTP buffering before JSON parsing, including chunked requests."""
import asyncio
from time import monotonic
from starlette.responses import JSONResponse


class RequestBodyLimit:
    def __init__(self, app, *, max_concurrent=4, body_timeout=60, rate=20, burst=40,
                 worker_verifier=None, allowed_workers=()):
        self.app = app
        self.max_concurrent = max_concurrent
        self.body_timeout = body_timeout
        self.rate, self.burst = rate, burst
        self.tokens, self.updated = float(burst), monotonic()
        self.active = 0
        self.worker_verifier = worker_verifier
        self.allowed_workers = frozenset(allowed_workers)
        self.auth_active = 0
        self.worker_lane = (RequestBodyLimit(app, body_timeout=5, rate=40, burst=80)
                            if worker_verifier is not None else None)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        if self.worker_lane is not None and scope['path'].startswith('/worker/'):
            # Authenticate before accepting a body or granting reserved capacity.
            # A worker-shaped URL or forged Authorization header is not sufficient.
            if self.auth_active >= 4:
                return await JSONResponse({'error': 'auth_busy'}, status_code=503)(scope, receive, send)
            self.auth_active += 1
            try:
                headers = dict(scope.get('headers', []))
                header = headers.get(b'authorization', b'').decode('latin-1')
                token = (await asyncio.wait_for(self.worker_verifier.verify_token(header[7:]), 6)
                         if header.startswith('Bearer ') else None)
            except Exception:
                token = None
            finally:
                self.auth_active -= 1
            if token is None or token.subject not in self.allowed_workers:
                return await JSONResponse({'error': 'unauthorized'}, status_code=401)(scope, receive, send)
            return await self.worker_lane(scope, receive, send)
        # Process-wide admission bounds: never trust forwarded IPs or store tokens.
        now = monotonic()
        self.tokens = min(self.burst, self.tokens + (now - self.updated) * self.rate)
        self.updated = now
        if self.tokens < 1:
            return await JSONResponse({"error": "rate_limited"}, status_code=429,
                                      headers={"Retry-After": "1"})(scope, receive, send)
        self.tokens -= 1
        if self.active >= self.max_concurrent:
            return await JSONResponse({"error": "server_busy"}, status_code=503,
                                      headers={"Retry-After": "1"})(scope, receive, send)
        self.active += 1
        try:
            await self._request(scope, receive, send)
        finally:
            self.active -= 1

    async def _request(self, scope, receive, send):
        # /mcp carries multi-image scans; app target registration
        # carries two
        # target photos — sized for the modern standard, not the
        # generic cap: two 7 MiB images base64-inflate to roughly
        # 19 MiB on the wire, so the endpoint allows 20 MiB.
        # Everything else stays at 1 MiB.
        path = scope["path"].rstrip("/")
        limit = (40 if path == "/mcp"
                 else 20 if path == "/api/app/targets"
                 else 1) * 1024 * 1024
        for name, value in scope.get("headers", []):
            if name.lower() == b"content-length":
                try:
                    length = int(value)
                except ValueError:
                    length = -1
                if length < 0 or length > limit:
                    return await JSONResponse({"error": "invalid_body_size"}, status_code=413)(scope, receive, send)
        body = bytearray()
        deadline = monotonic() + self.body_timeout
        while True:
            try:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise TimeoutError
                message = await asyncio.wait_for(receive(), timeout=remaining)
            except TimeoutError:
                return await JSONResponse({"error": "body_timeout"}, status_code=408)(scope, receive, send)
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > limit:
                return await JSONResponse({"error": "body_too_large"}, status_code=413)(scope, receive, send)
            body.extend(chunk)
            if not message.get("more_body", False):
                break
        delivered = False

        async def bounded_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, bounded_receive, send)
