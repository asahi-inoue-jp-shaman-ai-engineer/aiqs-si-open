"""Bound HTTP buffering before JSON parsing, including chunked requests.

Admission is laned (Astra final round #3; hardened in the batch 2c
diff review). A single process-wide pool let four slow
UNAUTHENTICATED requests deny the whole public application —
health, account operations and cancellation alike. Now:

- /health is never admission-controlled: liveness must always be
  answerable, even under a flood.
- The worker lane keeps its own admission exactly: the outer
  limiter verifies the worker token first, then the nested
  worker-only limiter applies its own rate bucket, slot pool and
  5-second body deadline. Verified worker traffic is never
  reclassified by header presence.
- The authenticated lane is reserved for requests whose Bearer
  token the account verifier accepts (verified BEFORE a slot is
  granted or a body is buffered; any failure — forged, expired,
  verifier busy — falls back to the public lane, and the
  application still answers its own 401). Verification is itself
  bounded: accepted tokens are cached briefly by hash, new
  verifications draw from a rate bucket and a small concurrent
  pool, so a forged-token flood can neither exhaust signature
  checking nor downgrade established sessions out of the lane.
  The lane has a larger slot pool, the full body timeout, and its
  own rate bucket. Cancellation and account operations ride this
  lane, so a public flood cannot starve them. Verification caches
  are separated per verifier domain (an account token never earns
  the worker lane, nor the reverse), and lapsed cache entries
  revalidate through a small reserved budget and pool instead of
  competing with unknown tokens. Compositions that
  pass no account verifier keep the legacy header-presence
  classification (development and tests only; the production
  compositions pass the verifier).
- Everything else uses the public lane: the historical small pool,
  a shorter body deadline (slow pre-auth uploads are the attack
  shape), a process-wide rate bucket, and a per-source rate
  bucket. Forwarded IPs are never trusted; the per-source key is
  the direct peer only, so behind a tunnel it degrades to a second
  process-wide bucket and the lane caps remain the real bound.
"""
import asyncio
import hashlib
from time import monotonic, time
from starlette.responses import JSONResponse
from .jwks import VerificationUnavailable

_HEALTH_PATHS = frozenset({"/health"})
_SOURCE_BUCKET_LIMIT = 4096
# A token a verifier accepted is remembered (by hash, never in
# clear): established sessions keep their lane even while the
# verification pool is saturated by forged tokens. A hit renews
# the entry's deadline (sliding), bounded by an absolute cap —
# the token's own expiry when the verifier exposes it, else ten
# minutes from verification — after which the token is verified
# again. The application still verifies every request itself;
# the cache governs admission priority only.
_VERIFY_CACHE_TTL = 60.0
_VERIFY_CACHE_CAP = 600.0
_VERIFY_CACHE_LIMIT = 1024
_REVERIFY_COOLDOWN = 30.0


class RequestBodyLimit:
    def __init__(self, app, *, max_concurrent=4, body_timeout=60, rate=20, burst=40,
                 worker_verifier=None, allowed_workers=(), account_verifier=None,
                 auth_max_concurrent=12, public_body_timeout=20,
                 source_rate=None, source_burst=None, worker_only=False,
                 worker_verify_rate=40, worker_verify_burst=80,
                 reading_max_concurrent=4):
        self.app = app
        self.max_concurrent = max_concurrent
        self.body_timeout = body_timeout
        self.rate, self.burst = rate, burst
        self.source_rate = rate if source_rate is None else source_rate
        self.source_burst = burst if source_burst is None else source_burst
        self.tokens, self.updated = float(burst), monotonic()
        self.active = 0
        self.worker_verifier = worker_verifier
        self.allowed_workers = frozenset(allowed_workers)
        self.account_verifier = account_verifier
        self.worker_only = worker_only
        self.auth_active = 0
        self.verify_active = 0
        self._verify_cache = {"account": {}, "worker": {}}
        self.verify_tokens, self.verify_updated = float(burst), monotonic()
        self.reverify_rate, self.reverify_burst = 5, 10
        self.reverify_tokens = float(self.reverify_burst)
        self.reverify_updated = monotonic()
        self.reverify_active = 0
        self._revalidating = set()
        # Worker-token verification has its own budget, mirroring
        # the nested worker lane's rate bucket: forged worker
        # tokens cannot buy unbounded signature checks either.
        self.worker_verify_rate = worker_verify_rate
        self.worker_verify_burst = worker_verify_burst
        self.worker_verify_tokens = float(worker_verify_burst)
        self.worker_verify_updated = monotonic()
        self.auth_max_concurrent = auth_max_concurrent
        self.authed_active = 0
        self.auth_tokens, self.auth_updated = float(burst), monotonic()
        # The pre-auth deadline never exceeds the configured body
        # timeout (the worker lane's 5-second budget stays 5).
        self.public_body_timeout = min(public_body_timeout, body_timeout)
        self._source_buckets = {}
        # Reading generation lane (batch 2b item 5): a reading holds
        # its admission slot for the whole provider wait, so readings
        # get their OWN bounded pool — four slow readings can no
        # longer occupy every general slot and 503 the app, the MCP
        # surface and /health with them. The token bucket stays
        # process-wide (one abuse bound), the concurrency pools are
        # separate.
        self.reading_max_concurrent = reading_max_concurrent
        self.reading_active = 0
        self.worker_lane = (RequestBodyLimit(app, body_timeout=5, rate=40, burst=80,
                                             worker_only=True)
                            if worker_verifier is not None else None)

    def _source_allows(self, scope):
        client = scope.get("client")
        key = client[0] if client else "unknown"
        now = monotonic()
        bucket = self._source_buckets.get(key)
        if bucket is None:
            if len(self._source_buckets) >= _SOURCE_BUCKET_LIMIT:
                self._source_buckets.pop(next(iter(self._source_buckets)))
            bucket = [float(self.source_burst), now]
            self._source_buckets[key] = bucket
        tokens = min(self.source_burst,
                     bucket[0] + (now - bucket[1]) * self.source_rate)
        bucket[1] = now
        if tokens < 1:
            bucket[0] = tokens
            return False
        bucket[0] = tokens - 1
        return True

    @staticmethod
    def _token_key(token):
        return hashlib.sha256(token.encode("utf-8", "ignore")).hexdigest()

    def _cache_state(self, domain, key):
        """(state, entry): 'hit' (valid; deadline renewed),
        'stale' (known token, entry lapsed — revalidation lane),
        'cooling' (lapsed, but a transient revalidation failure is
        still backing off), or 'miss'. Caches are per-domain: a
        token verified for one verifier never earns the other
        domain's lane. The entry object is returned so failure
        handling can be tied to the exact entry version that was
        revalidated: a late failure must never evict or mutate a
        fresher entry stored by a concurrent successful
        revalidation."""
        entry = self._verify_cache[domain].get(key)
        if entry is None:
            return "miss", None
        now = monotonic()
        deadline, cap, retry_after = entry
        if now < cap and now < deadline:
            entry[0] = min(now + _VERIFY_CACHE_TTL, cap)
            return "hit", entry
        if now < retry_after:
            return "cooling", entry
        return "stale", entry

    def _cache_store(self, domain, key, claims):
        now = monotonic()
        horizon = _VERIFY_CACHE_CAP
        expires_at = getattr(claims, "expires_at", None)
        if isinstance(expires_at, (int, float)) and not isinstance(expires_at, bool):
            horizon = min(horizon, max(0.0, expires_at - time()))
        if horizon <= 0:
            return
        cache = self._verify_cache[domain]
        if len(cache) >= _VERIFY_CACHE_LIMIT:
            cache.pop(next(iter(cache)))
        cap = now + horizon
        cache[key] = [min(now + _VERIFY_CACHE_TTL, cap), cap, 0.0]

    def _revalidation_failed(self, domain, key, entry, *, definitive):
        """Failure bookkeeping for one reserved revalidation
        attempt, tied to the entry version that was revalidated.
        A definitive rejection (the verifier answered and refused)
        evicts that entry: the credential gets one reserved
        attempt, and its next request pays the shared budget like
        any unknown token. A transient failure (verifier error or
        timeout) keeps the entry and backs off: the reserved lane
        retries it at most once per cooldown, so a verifier outage
        neither drains the reserve nor strips the session of its
        reserved retry once the verifier recovers. If a concurrent
        revalidation already replaced the entry, this failure
        belongs to a dead version and changes nothing."""
        cache = self._verify_cache[domain]
        if cache.get(key) is not entry:
            return
        if definitive:
            cache.pop(key, None)
        else:
            entry[2] = monotonic() + _REVERIFY_COOLDOWN

    def _revalidation_begin(self, domain, key):
        """Reserve one reserved-lane revalidation for this entry:
        the shared revalidation bucket and pool, plus a per-entry
        in-flight marker so concurrent requests for the SAME
        lapsed token coalesce onto a single attempt (the others
        fall back immediately)."""
        marker = (domain, key)
        if marker in self._revalidating:
            return False
        now = monotonic()
        self.reverify_tokens = min(
            self.reverify_burst,
            self.reverify_tokens + (now - self.reverify_updated) * self.reverify_rate)
        self.reverify_updated = now
        if self.reverify_tokens < 1 or self.reverify_active >= 2:
            return False
        self.reverify_tokens -= 1
        self._revalidating.add(marker)
        return True

    def _revalidation_end(self, domain, key):
        self._revalidating.discard((domain, key))

    @staticmethod
    async def _verify_strict(verifier, token):
        """(claims, transient) for a revalidation attempt. When
        the verifier offers a strict surface (JWKS), a
        VerificationUnavailable means the verification SERVICE
        could not answer — key endpoint down or backing off —
        which is transient; a plain None is a credential refusal.
        Without a strict surface, only exceptions/timeouts count
        as transient."""
        strict = getattr(verifier, "verify_token_strict", None)
        try:
            if strict is not None:
                return await asyncio.wait_for(strict(token), 6), False
            return await asyncio.wait_for(verifier.verify_token(token), 6), False
        except VerificationUnavailable:
            return None, True
        except Exception:
            return None, True

    async def _account_verified(self, scope):
        """True only when the account verifier accepts the request's
        Bearer token. Bounds keep verification itself from becoming
        the denial-of-service surface: a per-domain cache of
        accepted tokens (established sessions never re-pay
        verification and survive pool saturation), a reserved
        revalidation budget for lapsed entries, a token bucket over
        NEW verifications (a forged-token flood cannot make the
        service perform unbounded signature checks), and a small
        concurrent pool with a short timeout. Budget and pool are
        reserved together, synchronously, so a busy pool never
        wastes budget. On overload, error, or a missing/forged
        token the request simply falls back to the public lane."""
        headers = dict(scope.get("headers", []))
        header = headers.get(b"authorization", b"").decode("latin-1")
        if not header.startswith("Bearer "):
            return False
        token = header[7:]
        if not token.strip():
            return False
        key = self._token_key(token)
        state, entry = self._cache_state("account", key)
        if state == "hit":
            return True
        if state == "cooling":
            return False
        if state == "stale":
            if not self._revalidation_begin("account", key):
                return False
            self.reverify_active += 1
            try:
                claims, transient = await self._verify_strict(
                    self.account_verifier, token)
            finally:
                self.reverify_active -= 1
                self._revalidation_end("account", key)
            if claims is None:
                self._revalidation_failed("account", key, entry,
                                           definitive=not transient)
                return False
            self._cache_store("account", key, claims)
            return True
        now = monotonic()
        self.verify_tokens = min(self.burst, self.verify_tokens
                                 + (now - self.verify_updated) * self.rate)
        self.verify_updated = now
        if self.verify_tokens < 1 or self.verify_active >= 4:
            return False
        self.verify_tokens -= 1
        self.verify_active += 1
        try:
            claims = await asyncio.wait_for(
                self.account_verifier.verify_token(token), 6)
        except Exception:
            claims = None
        finally:
            self.verify_active -= 1
        if claims is None:
            return False
        self._cache_store("account", key, claims)
        return True

    @staticmethod
    def _is_reading_create(scope):
        # Reading creation waits on the provider inside the request
        # (batch 2b item 5).
        return (scope.get("method") == "POST"
                and scope["path"].rstrip("/") == "/api/app/readings")

    async def _serve_reading(self, scope, receive, send, *, body_timeout=None):
        # It draws from the reading pool only, whichever lane
        # classified the request, so slow readings can never occupy
        # the general or authenticated pools (batch 2b item 5, kept
        # under the batch 2c lane split).
        if self.reading_active >= self.reading_max_concurrent:
            return await JSONResponse({"error": "server_busy"}, status_code=503,
                                      headers={"Retry-After": "1"})(scope, receive, send)
        self.reading_active += 1
        try:
            await self._request(scope, receive, send,
                                body_timeout=body_timeout)
        finally:
            self.reading_active -= 1

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        if scope["path"] in _HEALTH_PATHS:
            # Liveness must answer even when every admission pool is
            # saturated (batch 2b item 5): no token, no slot, no body.
            return await self.app(scope, receive, send)
        if self.worker_only:
            # Verified worker traffic, delegated by the outer limiter
            # after token verification: this lane's own rate bucket,
            # slot pool and body deadline, no reclassification.
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
                await self._request(scope, receive, send,
                                    body_timeout=self.body_timeout)
            finally:
                self.active -= 1
            return
        if self.worker_lane is not None and scope['path'].startswith('/worker/'):
            # Authenticate before accepting a body or granting reserved capacity.
            # A worker-shaped URL or forged Authorization header is not sufficient.
            headers = dict(scope.get('headers', []))
            header = headers.get(b'authorization', b'').decode('latin-1')
            raw = header[7:] if header.startswith('Bearer ') else None
            if raw is None or not raw.strip():
                return await JSONResponse({'error': 'unauthorized'}, status_code=401)(scope, receive, send)
            key = self._token_key(raw)
            state, entry = self._cache_state("worker", key)
            if state == "hit":
                return await self.worker_lane(scope, receive, send)
            if state == "cooling":
                return await JSONResponse({'error': 'auth_busy'}, status_code=503)(scope, receive, send)
            if state == "stale":
                # Established worker, lapsed entry: revalidate on
                # the reserved budget/pool, never against the flood.
                if not self._revalidation_begin("worker", key):
                    return await JSONResponse({'error': 'auth_busy'}, status_code=503)(scope, receive, send)
                self.reverify_active += 1
                try:
                    token, transient = await self._verify_strict(
                        self.worker_verifier, raw)
                finally:
                    self.reverify_active -= 1
                    self._revalidation_end("worker", key)
                if token is None or token.subject not in self.allowed_workers:
                    self._revalidation_failed(
                        "worker", key, entry,
                        definitive=not transient)
                    return await JSONResponse({'error': 'unauthorized'}, status_code=401)(scope, receive, send)
                self._cache_store("worker", key, token)
                return await self.worker_lane(scope, receive, send)
            # Verification work is budgeted (mirroring the worker
            # lane's rate bucket) before it is pooled: forged worker
            # tokens cannot buy unbounded signature checks. Budget
            # and pool are reserved together, synchronously, so a
            # busy pool never wastes budget.
            now = monotonic()
            self.worker_verify_tokens = min(
                self.worker_verify_burst,
                self.worker_verify_tokens
                + (now - self.worker_verify_updated) * self.worker_verify_rate)
            self.worker_verify_updated = now
            if self.worker_verify_tokens < 1:
                return await JSONResponse({'error': 'rate_limited'}, status_code=429,
                                          headers={'Retry-After': '1'})(scope, receive, send)
            if self.auth_active >= 4:
                return await JSONResponse({'error': 'auth_busy'}, status_code=503)(scope, receive, send)
            self.worker_verify_tokens -= 1
            self.auth_active += 1
            try:
                token = await asyncio.wait_for(self.worker_verifier.verify_token(raw), 6)
            except Exception:
                token = None
            finally:
                self.auth_active -= 1
            if token is None or token.subject not in self.allowed_workers:
                return await JSONResponse({'error': 'unauthorized'}, status_code=401)(scope, receive, send)
            self._cache_store("worker", key, token)
            return await self.worker_lane(scope, receive, send)
        # Lane selection: with an account verifier, only a token it
        # accepts earns the authenticated lane (verified before any
        # slot or buffering); without one (development/tests), the
        # legacy header-presence classification applies.
        if self.account_verifier is not None:
            authed = await self._account_verified(scope)
        else:
            authed = any(name.lower() == b"authorization"
                         for name, _ in scope.get("headers", []))
        if authed:
            now = monotonic()
            self.auth_tokens = min(self.burst, self.auth_tokens
                                   + (now - self.auth_updated) * self.rate)
            self.auth_updated = now
            if self.auth_tokens < 1:
                return await JSONResponse({"error": "rate_limited"}, status_code=429,
                                          headers={"Retry-After": "1"})(scope, receive, send)
            self.auth_tokens -= 1
            if self._is_reading_create(scope):
                return await self._serve_reading(
                    scope, receive, send, body_timeout=self.body_timeout)
            if self.authed_active >= self.auth_max_concurrent:
                return await JSONResponse({"error": "server_busy"}, status_code=503,
                                          headers={"Retry-After": "1"})(scope, receive, send)
            self.authed_active += 1
            try:
                await self._request(scope, receive, send,
                                    body_timeout=self.body_timeout)
            finally:
                self.authed_active -= 1
            return
        # Public lane: process-wide rate bucket, then a per-source
        # bucket, then the small slot pool with the short deadline.
        now = monotonic()
        self.tokens = min(self.burst, self.tokens + (now - self.updated) * self.rate)
        self.updated = now
        if self.tokens < 1:
            return await JSONResponse({"error": "rate_limited"}, status_code=429,
                                      headers={"Retry-After": "1"})(scope, receive, send)
        self.tokens -= 1
        if not self._source_allows(scope):
            return await JSONResponse({"error": "rate_limited"}, status_code=429,
                                      headers={"Retry-After": "1"})(scope, receive, send)
        if self._is_reading_create(scope):
            return await self._serve_reading(scope, receive, send)
        if self.active >= self.max_concurrent:
            return await JSONResponse({"error": "server_busy"}, status_code=503,
                                      headers={"Retry-After": "1"})(scope, receive, send)
        self.active += 1
        try:
            await self._request(scope, receive, send,
                                body_timeout=self.public_body_timeout)
        finally:
            self.active -= 1

    async def _request(self, scope, receive, send, *, body_timeout=None):
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
        deadline = monotonic() + (self.body_timeout if body_timeout is None
                                  else body_timeout)
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
