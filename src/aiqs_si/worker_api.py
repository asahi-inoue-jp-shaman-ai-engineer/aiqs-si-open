"""Private pull-worker API; never exposes user account or credit mutation tools."""
import json
import logging
import time
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

WORKER_SCOPE = "aiqs:worker"

logger = logging.getLogger(__name__)

# Worker-contact recording is fail-soft telemetry (it must never
# fail a claim), but a persistent failure should not be invisible:
# log the exception TYPE at most once per interval — never tokens,
# identities, or payloads.
_CONTACT_LOG_INTERVAL_SEC = 300
_last_contact_failure_log = None  # monotonic timestamp


def _note_contact(store):
    global _last_contact_failure_log
    try:
        store.note_worker_contact()
    except Exception as error:  # telemetry only; see module note
        now = time.monotonic()
        if (_last_contact_failure_log is None
                or now - _last_contact_failure_log
                >= _CONTACT_LOG_INTERVAL_SEC):
            _last_contact_failure_log = now
            logger.warning("worker contact recording failed (%s)",
                           type(error).__name__)


def create_worker_app(store, verifier, *, allowed_subjects):
    if verifier.required_scope != WORKER_SCOPE:
        raise ValueError("A worker-specific verifier is required")
    allowed = frozenset(allowed_subjects)
    if not allowed:
        raise ValueError("Explicit worker identities required")

    async def dispatch(request):
        header = request.headers.get("authorization", "")
        token = await verifier.verify_token(header[7:]) if header.startswith("Bearer ") else None
        if token is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if token.subject not in allowed:
            return JSONResponse({"error": "forbidden"}, status_code=403)
        identity = json.dumps([token.issuer, token.subject], separators=(",", ":"))
        try:
            if request.url.path == "/worker/claim":
                # The claim body is optional; when present it may carry
                # the worker's diode identity (SHA-256 prefix hash of the
                # serial — the raw serial never leaves the worker host).
                try:
                    claim_body = await request.json()
                except ValueError:
                    claim_body = None
                if not isinstance(claim_body, dict):
                    claim_body = {}
                job = store.claim_next(worker=identity,
                                       diode_serial_hash=claim_body.get("diode_serial_hash"))
                # Presence evidence for the duration estimator: a
                # claim just SUCCEEDED (a job, or a healthy empty
                # queue), so the lane is known to be moving. Failed
                # claims record nothing — a worker that can never
                # claim must not keep the basis alive. Telemetry
                # only: a contact failure never fails the claim.
                _note_contact(store)
                return JSONResponse({"job": job})
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("Object required")
            job_id = body["job_id"]
            if not isinstance(job_id, str):
                raise ValueError("Job ID required")
            # Catalog lookup is bound to this worker's claimed job, never a caller digest.
            if request.url.path in ("/worker/catalog", "/worker/context"):
                with store.transaction() as db:
                    row = db.execute("SELECT jobs.payload FROM jobs JOIN worker_claims ON jobs.id=worker_claims.job WHERE jobs.id=? AND worker_claims.worker=? AND jobs.state='running'",
                                     (job_id, identity)).fetchone()
                    if row is None:
                        raise PermissionError("Job not assigned")
                    # Claim binding (plan v4 section 8): a presented
                    # claim generation must match the live claim in
                    # this same transaction (generation + expiry).
                    store._verify_claim(db, job_id, identity,
                                        body.get("claim_generation"))
                if request.url.path == "/worker/context":
                    return JSONResponse({"context": store.get_context(
                        job_id, worker=identity,
                        claim_generation=body.get("claim_generation"))})
                digest = json.loads(row["payload"])["catalog_digest"]
                catalog = store.get_catalog(digest)
                return JSONResponse({"digest": digest, "fields": [
                    {"name": name, "entries": entries} for name, entries in catalog.items()]})
            if request.url.path == "/worker/control":
                return JSONResponse(store.execution_control(
                    job_id, body.get("completed"), worker=identity,
                    claim_generation=body.get("claim_generation")))
            if request.url.path == '/worker/heartbeat':
                store.heartbeat(job_id, worker=identity,
                                claim_generation=body.get("claim_generation"))
                # A live execution heartbeat is also presence: the
                # claim poll pauses while a job executes, which can
                # far exceed the estimator's presence window.
                _note_contact(store)
                return JSONResponse({'ok': True})
            if request.url.path == "/worker/scan-receipt":
                store.reconcile_scan_receipt(job_id, body['digest'], worker=identity,
                                             claim_generation=body.get("claim_generation"))
                return JSONResponse({'ok': True})
            store.finish(job_id, body["state"], body["result"], worker=identity,
                         diode_serial_hash=body.get("diode_serial_hash"),
                         claim_generation=body.get("claim_generation"))
            return JSONResponse({"ok": True})
        except PermissionError:
            return JSONResponse({"error": "forbidden"}, status_code=403)
        except (ValueError, KeyError, TypeError):
            return JSONResponse({"error": "invalid_request_or_state"}, status_code=409)

    return Starlette(routes=[Route(path, dispatch, methods=["POST"]) for path in
                            ("/worker/claim", "/worker/catalog", "/worker/context", "/worker/control", "/worker/heartbeat", "/worker/scan-receipt", "/worker/finish")])
