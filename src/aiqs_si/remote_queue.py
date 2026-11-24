"""HTTPS pull transport for the dedicated worker. Does not retry device jobs."""
import hashlib
import json
from urllib.parse import urlsplit
import httpx


class RemoteQueue:
    def __init__(self, base_url, token_provider, *, client=None, diode_serial_hash=None,
                 boundary_key=None):
        parsed = urlsplit(base_url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment
                or parsed.path not in ("", "/")):
            raise ValueError("Worker service origin must be an HTTPS URL")
        # Device evidence for execution proof: sha256(serial) truncated
        # to 24 hex chars, the same derivation as the local log names.
        # The raw serial is never sent to the server.
        if diode_serial_hash is not None and (
                not isinstance(diode_serial_hash, str) or len(diode_serial_hash) != 24
                or any(c not in "0123456789abcdef" for c in diode_serial_hash)):
            raise ValueError("Diode serial hash must be a 24-char SHA-256 hex prefix")
        self.base_url = base_url.rstrip("/")
        self.token_provider = token_provider
        # Worker boundary credential (batch 2d, HIGH 3): when the
        # host's credential environment supplies one, every request
        # carries it so the service can verify this host's tokens
        # through the reserved channel a forged-token flood cannot
        # starve. The value is a credential: it is only ever placed
        # in this header, never logged or put in an error.
        self.boundary_key = boundary_key or None
        self.client = client or httpx.Client(timeout=15, follow_redirects=False, trust_env=False)
        self.diode_serial_hash = diode_serial_hash
        self.active_job = None
        # Claim binding (plan v4 section 8): the generation issued
        # with the active claim, presented on every follow-up call.
        self.claim_generation = None

    def close(self):
        self.client.close()

    def _post(self, path, body):
        headers = {"Authorization": "Bearer " + self.token_provider()}
        if self.boundary_key:
            headers["X-Si-Worker-Boundary"] = self.boundary_key
        try:
            response = self.client.post(self.base_url + path, json=body,
                                       headers=headers,
                                       follow_redirects=False)
        except httpx.TransportError:
            raise ConnectionError("Worker queue transport unavailable") from None
        if response.status_code != 200:
            # Never include response bodies, bearer tokens or private job content in errors.
            raise ConnectionError("Worker queue request rejected")
        return response.json()

    def claim_next(self):
        body = {"diode_serial_hash": self.diode_serial_hash} if self.diode_serial_hash else {}
        job = self._post("/worker/claim", body)["job"]
        self.active_job = job
        self.claim_generation = job.get("claim_generation") if job else None
        return job

    def _claim_fields(self):
        if type(self.claim_generation) is int:
            return {"claim_generation": self.claim_generation}
        return {}

    def get_catalog(self, digest):
        if self.active_job is None or self.active_job["payload"].get("catalog_digest") != digest:
            raise ValueError("Catalog must belong to the active job")
        data = self._post("/worker/catalog",
                          {"job_id": self.active_job["id"], **self._claim_fields()})
        encoded = json.dumps(data["fields"], sort_keys=True, separators=(",", ":"), allow_nan=False)
        if data["digest"] != digest or hashlib.sha256(encoded.encode()).hexdigest() != digest:
            raise ValueError("Remote catalog integrity failure")
        return {field["name"]: field["entries"] for field in data["fields"]}

    def finish(self, job, state, result, *, claim_generation=None):
        # An explicit generation (an outbox replay presenting the
        # generation recorded with the result, batch 2b item 2)
        # overrides the active claim's; the live path passes none.
        fields = {"claim_generation": claim_generation} \
            if type(claim_generation) is int else self._claim_fields()
        body = {"job_id": job, "state": state, "result": result, **fields}
        if self.diode_serial_hash:
            body["diode_serial_hash"] = self.diode_serial_hash
        if self._post("/worker/finish", body) != {"ok": True}:
            raise ConnectionError("Worker completion not acknowledged")

    def execution_control(self, job, completed=None):
        return self._post("/worker/control",
                          {"job_id": job, "completed": completed, **self._claim_fields()})

    def heartbeat(self, job):
        if self._post('/worker/heartbeat', {'job_id': job, **self._claim_fields()}) != {'ok': True}:
            raise ConnectionError('Heartbeat not acknowledged')

    def reconcile_scan_receipt(self, job, digest, *, claim_generation=None):
        fields = {"claim_generation": claim_generation} \
            if type(claim_generation) is int else self._claim_fields()
        if self._post("/worker/scan-receipt",
                      {"job_id": job, "digest": digest, **fields}) != {'ok': True}:
            raise ConnectionError("Scan receipt not acknowledged")

    def get_context(self, job):
        if self.active_job is None or self.active_job["id"] != job:
            raise ValueError("Context must belong to the active job")
        return self._post("/worker/context",
                          {"job_id": job, **self._claim_fields()})["context"]
