"""User-session job API for the standalone "アイクスSI アプリ"
(si-standalone-app-design-20261122, route (b) in §5.2).

The browser counterpart of the NPC delegated jobs API: the same
production job machinery (Service submission, settlement, execution
proof, fixed error codes), but the caller is the user themself,
authenticated with the same verified Bearer session as the account
API — never the shared link key, never a D-Planet player. The
account is resolved from the verified token (Keycloak subject ->
SI account, the account API's own resolution), so the app works
for standalone accounts exactly as for linked ones; while linked,
credit routing keeps the balance in D-Planet, handled inside the
Store as for every other channel.

Only jobs attributed in app_jobs are visible or actionable here:
a job the owner created through their agent (MCP) is not an app
session and does not leak into the app surface, and another user's
job answers 404 exactly like the internal API's foreign-job rule.

Irradiation requires a server-side consent record (§5.4): the
confirmation screen (S5) is backed by POST .../irradiate/prepare,
which parks the shown quote as a one-shot confirmation; the
irradiate call is accepted only against a live confirmation and
the charge is checked against the parked quote before submission,
so a price change returns the user to the confirmation screen
instead of silently charging a different amount.

Rollout is gated by AIQS_SI_APP_ENABLED (the registration-switch
pattern): while unset the routes answer 404, the safe pre-launch
state, and the /app page is not served either (web_ui).
"""
import asyncio
import json
import os
import sqlite3
import time
import uuid
from datetime import datetime, timezone

from starlette.responses import JSONResponse
from starlette.routing import Route

from .auth import resolve_account
from .dplanet import DPlanetError
from .estimates import (current_policy as current_estimate_policy,
                        duration_estimate)
from .pricing import (CAMPAIGN_ACTIVE, CAMPAIGN_NAME, CREDITS_PER_SHOT,
                      MAX_SHOTS, PRICE_VERSION, READING_CREDITS,
                      active_credits_per_shot, active_rate_tenths,
                      irradiation_quote, reading_quote)
from .reading import ReadingGenerationError
from .referral import ReferralError
from .scan_rejections import scan_error_code
from .serializers import public_job_view
from .store import ReadingChargeError

APP_ENABLED_ENV = "AIQS_SI_APP_ENABLED"
REFERRAL_ENABLED_ENV = "AIQS_SI_REFERRAL_ENABLED"
CONFIRMATION_TTL_SECONDS = 600


def app_enabled_from_env(environ=None):
    """Operator switch for the standalone app route; default OFF."""
    environ = os.environ if environ is None else environ
    value = environ.get(APP_ENABLED_ENV)
    return isinstance(value, str) and value.strip().lower() in ("1", "true", "yes", "on")


def referral_enabled_from_env(environ=None):
    """Operator switch for the referral system; default OFF.

    While off, the referral routes answer 404 and the session
    payload reports the feature as disabled, so the app hides the
    referral card and the subscription banner entirely.
    """
    environ = os.environ if environ is None else environ
    value = environ.get(REFERRAL_ENABLED_ENV)
    return isinstance(value, str) and value.strip().lower() in ("1", "true", "yes", "on")


def _iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def app_routes(store, verifier, *, service, dplanet_client=None,
               dplanet_routing=False, enabled=True,
               reading_service=None, reading_enabled=False,
               referral_enabled=False, notify_configured=False,
               estimate_policy=None):
    """User-authenticated app routes for the combined application.

    The reading routes (native-reading-design-20261122) exist only
    while reading_enabled and a reading service are both present;
    otherwise they answer 404 like every other gated surface, and
    the session payload reports the feature as disabled."""

    def denied():
        return JSONResponse({"error": "not_found"}, status_code=404)

    async def authenticate(request):
        """(owner, token) from the verified Bearer token, or (None, None)."""
        header = request.headers.get("authorization", "")
        token = await verifier.verify_token(header[7:]) if header.startswith("Bearer ") else None
        if token is None:
            return None, None
        return resolve_account(store, token), token

    async def body_object(request):
        try:
            body = await request.json()
        except ValueError:
            return None
        return body if isinstance(body, dict) else None

    def request_key_of(body):
        key = body.get("request_key")
        return key if isinstance(key, str) and 1 <= len(key) <= 128 else None

    def completion_choice(body):
        """The per-submission wait/notify choice (job-notify-resume
        design). Absent means wait — the historical behaviour — and
        anything malformed is a client error, never a silent guess:
        the API must not invent a delivery preference."""
        raw = body.get("completion")
        if raw is None:
            return "wait"
        if not isinstance(raw, dict):
            raise ValueError("invalid_completion_mode")
        mode = raw.get("mode", "wait")
        if mode not in ("wait", "notify"):
            raise ValueError("invalid_completion_mode")
        return mode

    def notify_available(token):
        """Notify mode can be honoured only when delivery is
        configured AND the verified access token carries the user's
        own email claim — the only address the service may use."""
        return bool(notify_configured and token is not None
                    and getattr(token, "email", None))

    def completion_view(job_id):
        """The choice as recorded (first record wins), plus the
        notification lifecycle state. The address itself is never
        part of any response. `deliverable` is the per-job mail
        eligibility the client may promise on (combined review
        finding 2): a promise state (armed / queued / sending)
        can actually be mailed only while delivery is configured
        AND the record carries an address AND the result has not
        been viewed — a wait-mode job recorded without an address
        sits armed forever, because the escalation requires one,
        and a viewed record is suppressed by the delivery gate,
        so the client must not promise either mail."""
        row = store.job_notification(job_id)
        if row is None:
            return {"mode": "wait", "notification": "none",
                    "deliverable": False}
        # A viewed record is not deliverable either (round 2):
        # the delivery gate suppresses every viewed record, but
        # the view ack deliberately keeps a 'sending' record's
        # status and address (the mail may already be on the
        # wire), so eligibility must consult the view stamp
        # itself, not just status + address.
        deliverable = bool(
            notify_configured
            and row["status"] in ("armed", "queued", "sending")
            and row["email"]
            and row["viewed_at"] is None)
        return {"mode": row["mode"], "notification": row["status"],
                "deliverable": deliverable}

    active_estimate_policy = estimate_policy if estimate_policy is not None \
        else current_estimate_policy()

    def estimate_sync(operation, count=None, *, before_job_id=None,
                      owner=None):
        """The duration estimate for a confirmation or waiting
        screen (foundation item 3): a range in seconds including
        the queue wait, or None when the server has no honest basis
        (estimate policy unset, no worker known to be polling, the
        lane blocked by an unresolved execution, an unreadable job
        ahead). A missing basis never
        raises here: the estimate is display material, and its
        absence must not fail the screen it decorates."""
        try:
            return duration_estimate(
                store, operation, count, policy=active_estimate_policy,
                before_job_id=before_job_id, owner=owner)
        except (KeyError, ValueError, TypeError, RecursionError,
                sqlite3.Error):
            # sqlite3.Error included: the estimate decorates screens
            # that may already have committed a paid submission, and
            # a transient database lock must not turn that into a
            # 500 (Antigravity glass audit, finding 9).
            return None

    def balance_view(owner):
        """The credit_balance semantics of the MCP surface: while
        routing is active and the account is linked, the balance is
        read from D-Planet and an unreachable D-Planet is an error,
        never a silent substitution of the SI-side figure."""
        link = store.active_account_link(owner)
        view = {"linked": link is not None,
                "credits_per_shot": active_credits_per_shot(),
                "credits_per_shot_tenths": active_rate_tenths(),
                "standard_credits_per_shot": CREDITS_PER_SHOT,
                "campaign": CAMPAIGN_NAME if CAMPAIGN_ACTIVE else None,
                "price_version": PRICE_VERSION}
        if dplanet_routing and link is not None:
            view["balance_source"] = "dplanet"
            if dplanet_client is None:
                view["available"] = None
                view["error"] = "D-Planet balance routing is enabled but not configured"
                return view
            try:
                remote = dplanet_client.balance(link["dplanet_player_id"])
            except DPlanetError as error:
                view["available"] = None
                view["error"] = error.message
                return view
            view["available"] = remote.get("balance")
            return view
        view["balance_source"] = "si_local"
        view["available"] = store.balance(owner)
        return view

    def degraded_job_view(owner, job_id, attributed):
        """job_view when the stored payload no longer decodes
        (legacy corruption): the job's columns and the durable
        display summary still answer, and every payload- or
        proof-derived field is null — unavailable, never invented.
        Strict paths (execution, admission, the proof endpoint)
        keep failing on such a payload; only this owner-facing
        read degrades."""
        columns = store.job_columns(owner, job_id)
        display = store.display_summary(owner, {"id": job_id})
        queue = store.queue_status(owner, job_id)
        return {"job_id": job_id, "operation": None,
                "state": columns["state"], "cost": columns["cost"],
                "result": None,
                "intention": display["intention"],
                "self_love_joy": display["self_love_joy"],
                "targets": display["targets"],
                "content_removed": False,
                # The notification record is independent of the
                # unreadable payload, so the completion view is
                # still answerable — and must be present: a view
                # without it once let the client fall back to a
                # session-level mail promise (round 2, F2).
                "completion": completion_view(job_id),
                "execution": None, "device": None, "settlement": None,
                "proof_digest": None,
                "consent": attributed["consent"] if attributed else None,
                # The payload is unreadable, so this job's own
                # duration basis is gone with it: no estimate.
                "estimate": None,
                "queue": {"position": queue["position"],
                          "jobs_ahead": queue["jobs_ahead"]}}

    def job_view(owner, job_id):
        """The app status payload for one of the owner's app jobs:
        the public serializer's job view plus the public proof
        bundle's display fields (execution/device/settlement/digest),
        mirroring the internal API's status shape minus player data."""
        attributed = store.app_job(job_id)
        try:
            job = store.get(owner, job_id)
            # This read is SIDE-EFFECT FREE (batch 2d, MEDIUM 6):
            # it used to record "the user saw the result" itself,
            # so the status polling behind the wait screens — a
            # GET every few seconds — marked every finished job
            # viewed the moment it completed, and a user who had
            # closed the screen before that poll never received
            # the escalation mail the wait choice promised. The
            # view is now recorded only by the explicit ack
            # (POST /jobs/{id}/viewed), which the client sends
            # when a terminal result is actually rendered.
            proof = store.execution_proof(owner, job_id)
            view = public_job_view(store, job)
            queue = store.queue_status(owner, job_id)
            display = store.display_summary(owner, job)
            # The remaining-time estimate is offered only while the
            # job is still queued: once it is running, the server
            # holds no durable start it could bound the remainder
            # from, and a terminal job needs no estimate at all.
            payload = job.get("payload")
            estimate = estimate_sync(
                view.get("operation"),
                payload.get("irradiation_count")
                if isinstance(payload, dict) else None,
                before_job_id=job_id, owner=owner) \
                if job["state"] == "queued" else None
            result_view = {
                "job_id": job_id, "operation": view.get("operation"),
                "state": job["state"], "cost": job["cost"],
                "result": view.get("result"),
                "completion": completion_view(job_id),
                "estimate": estimate,
                # The user's own words, returned to themself only:
                # the §4.2 copy format heads the ranking with the
                # intention and self-love affirmation the user typed.
                # The display summary is the durable submit-time
                # snapshot, so the confirmation screen's "show
                # everything" (targets included) also holds for a
                # scan reopened from history after its content
                # expired; jobs predating summaries fall back to the
                # payload while it survives.
                "intention": display["intention"],
                "self_love_joy": display["self_love_joy"],
                "targets": display["targets"],
                # Whether the job's content has been removed by
                # the 24-hour scan expiry (batch 2d, MEDIUM 8): a
                # completed scan whose payload is the expiry
                # tombstone has no ranking left to show, and the
                # client must say so instead of rendering an
                # empty "success" ranking.
                "content_removed": bool(
                    job["payload"].get("content_removed")),
                "execution": proof["execution"], "device": proof["device"],
                "settlement": proof["settlement"],
                "proof_digest": proof["proof_digest"],
                "consent": attributed["consent"] if attributed else None,
                "queue": {"position": queue["position"],
                          "jobs_ahead": queue["jobs_ahead"]}}
            # Encoding preflight with the exact options the
            # response renderer (Starlette JSONResponse) uses: a
            # payload-derived string carrying an unpaired
            # surrogate (legacy storage) decodes and serializes
            # fine with ASCII escaping, but fails strict UTF-8
            # encoding at render time — after this function has
            # returned. Preflighting inside the guard degrades
            # such a view instead of failing the response.
            json.dumps(result_view, ensure_ascii=False, allow_nan=False,
                       separators=(",", ":")).encode("utf-8")
        except (ValueError, AttributeError, TypeError, RecursionError):
            # ValueError, not just JSONDecodeError: decoding can
            # also fail on oversized integers, proof serialization
            # rejects non-finite payload numbers, and the encoding
            # preflight raises UnicodeEncodeError (a ValueError)
            # on unencodable payload-derived strings — all are
            # stored-data failures this read must survive. The
            # degraded read is side-effect free for the same
            # reason as the strict one (batch 2d, MEDIUM 6).
            return degraded_job_view(owner, job_id, attributed)
        return result_view

    async def session(request):
        if not enabled:
            return denied()
        owner, _token = await authenticate(request)
        if owner is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        view = await asyncio.to_thread(balance_view, owner)
        view["reading"] = {"enabled": bool(reading_enabled and reading_service),
                           "credits": READING_CREDITS}
        view["referral"] = {"enabled": bool(referral_enabled)}
        # Whether the notify choice can be offered at all on this
        # account right now (delivery configured + verified email
        # claim). The UI hides the choice when this is false instead
        # of letting the user pick something the service cannot do.
        view["notify"] = {"available": notify_available(_token)}
        # Whether a duration estimate can be offered per operation
        # (the operator has set a measured effective duration). The
        # UI hides the estimate area when false; an estimate call
        # may still answer null for a concrete request when the
        # lane state removes the basis.
        view["estimate"] = {
            "scan": active_estimate_policy.scan_seconds is not None,
            "irradiation": (active_estimate_policy
                            .irradiation_seconds_per_shot is not None)}
        return JSONResponse(view)

    async def targets_list(request):
        if not enabled:
            return denied()
        owner, _token = await authenticate(request)
        if owner is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse({"targets": await asyncio.to_thread(store.list_targets, owner)})

    async def targets_register(request):
        if not enabled:
            return denied()
        owner, _token = await authenticate(request)
        if owner is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if service is None:
            return denied()
        body = await body_object(request)
        if body is None:
            return JSONResponse({"error": "JSON object required"}, status_code=400)
        request_key = request_key_of(body)
        if request_key is None:
            return JSONResponse({"error": "request_key required"}, status_code=400)
        try:
            result = await asyncio.to_thread(
                service.register_target, owner, request_key,
                name=body.get("name"), birthday=body.get("birthday") or "",
                gender=body.get("gender") or "",
                photo_base64=body.get("photo_base64") or "",
                photo2_base64=body.get("photo2_base64") or "")
        except (ValueError, TypeError):
            return JSONResponse({"error": "target_rejected"}, status_code=400)
        return JSONResponse(result)

    async def targets_rename(request):
        if not enabled:
            return denied()
        owner, _token = await authenticate(request)
        if owner is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        body = await body_object(request)
        target_id = body.get("target_id") if body is not None else None
        if not isinstance(target_id, str) or not target_id:
            return JSONResponse({"error": "target_id required"}, status_code=400)
        try:
            await asyncio.to_thread(store.rename_target, owner, target_id,
                                    body.get("name"))
        except KeyError:
            return JSONResponse({"error": "Target not found"}, status_code=404)
        except (ValueError, TypeError):
            return JSONResponse({"error": "target_rejected"}, status_code=400)
        return JSONResponse({"renamed": True, "target_id": target_id})

    async def targets_delete(request):
        if not enabled:
            return denied()
        owner, _token = await authenticate(request)
        if owner is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        body = await body_object(request)
        target_id = body.get("target_id") if body is not None else None
        if not isinstance(target_id, str) or not target_id:
            return JSONResponse({"error": "target_id required"}, status_code=400)
        try:
            await asyncio.to_thread(store.delete_target, owner, target_id)
        except KeyError:
            return JSONResponse({"error": "Target not found"}, status_code=404)
        return JSONResponse({"deleted": True, "target_id": target_id})

    async def job_estimate(request):
        """Pre-submission duration estimate for a confirmation
        screen (foundation item 3): a range in seconds including
        the queue wait, or estimate=null when the server has no
        honest basis for one. Asking creates and reserves nothing;
        the confirmation screens for irradiation also receive the
        same estimate inline from prepare, so this endpoint mainly
        serves the scan confirmation, which has no prepare step."""
        if not enabled:
            return denied()
        owner, _token = await authenticate(request)
        if owner is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        operation = request.query_params.get("operation")
        if operation not in ("scan", "irradiation"):
            return JSONResponse({"error": "invalid_operation"},
                                status_code=400)
        count = None
        if operation == "irradiation":
            raw_count = request.query_params.get("count")
            try:
                count = int(raw_count) if raw_count is not None else None
            except ValueError:
                count = None
            if type(count) is not int or not 1 <= count <= MAX_SHOTS:
                return JSONResponse({"error": "invalid_count"},
                                    status_code=400)
        estimate = await asyncio.to_thread(estimate_sync, operation, count)
        return JSONResponse({"operation": operation, "estimate": estimate})

    async def jobs_scan(request):
        if not enabled:
            return denied()
        owner, token = await authenticate(request)
        if owner is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if service is None:
            return denied()
        body = await body_object(request)
        if body is None:
            return JSONResponse({"error": "JSON object required"}, status_code=400)
        request_key = request_key_of(body)
        if request_key is None:
            return JSONResponse({"error": "request_key required"}, status_code=400)
        try:
            mode = completion_choice(body)
        except ValueError:
            return JSONResponse({"error": "invalid_completion_mode"},
                                status_code=400)
        existing = await asyncio.to_thread(store.get_by_request, owner, request_key)
        intent = await asyncio.to_thread(store.app_submission_intent,
                                         owner, request_key)
        if intent is not None and intent["operation"] != "scan":
            # The key is bound to a different operation's intent
            # (Astra round-5 F13): operation is part of the binding,
            # so this key can never serve a scan.
            return JSONResponse({"error": "request_key already used"}, status_code=409)
        if existing is not None and (await asyncio.to_thread(
                store.app_job, existing["id"])) is None and not (
                intent is not None
                and intent["intent_id"]
                and existing["payload"].get("operation") == "scan"
                and existing["payload"].get("app_intent")
                == intent["intent_id"]):
            # The key belongs to another channel; never re-attribute.
            # The one exception is our own crashed attempt, proven —
            # not merely suggested — by the intent id stamped into
            # the job's payload at creation: attribution, with the
            # intent's original choice, is completed below instead
            # of refused. An intent alone proves nothing (a rejected
            # submission leaves one behind only until it is
            # discarded, and it is never bound to a foreign job).
            return JSONResponse({"error": "request_key already used"}, status_code=409)
        created_intent = False
        if existing is None:
            # Persist the intent BEFORE the job is created, so the
            # original choice survives any crash from here on and
            # recovery never has to invent a preference. The row
            # that WINS the insert governs — a concurrent or earlier
            # attempt's choice is authoritative, not this request's
            # presentation — and the availability policy applies to
            # the WINNER (Astra round-5 F2): a durable notify intent
            # is refused while delivery cannot honour it, whatever
            # this request presented, and a durable wait intent is
            # never blocked by a presented notify.
            intent, created_intent = await asyncio.to_thread(
                store.record_app_submission_intent, owner, request_key,
                operation="scan", mode=mode,
                email=getattr(token, "email", None))
            if intent["operation"] != "scan":
                # The WINNING row is another operation's intent,
                # inserted after this request's initial lookup
                # (Astra round-6 F13): the binding is checked on
                # the winner, never only on the earlier read.
                return JSONResponse({"error": "request_key already used"},
                                    status_code=409)
            if intent["mode"] == "notify" and not (
                    notify_configured and intent["email"]):
                if created_intent:
                    await asyncio.to_thread(
                        store.discard_app_submission_intent, owner,
                        request_key, intent["intent_id"])
                return JSONResponse({"error": "notify_unavailable"},
                                    status_code=400)

        async def discard_own_intent():
            if created_intent:
                await asyncio.to_thread(
                    store.discard_app_submission_intent, owner,
                    request_key, intent["intent_id"])

        try:
            job = await asyncio.to_thread(
                service.submit_scan, owner, request_key, body.get("intention"),
                body.get("target_ids"), self_love_joy=body.get("self_love_joy"),
                app_intent_id=intent["intent_id"] if intent else None)
        except KeyError:
            await discard_own_intent()
            return JSONResponse({"error": "Target not found"}, status_code=404)
        except (ValueError, TypeError) as error:
            await discard_own_intent()
            # Typed rejection codes (batch 2b item 7): quota, budget
            # and configuration failures are distinct from malformed
            # input, so the app can say what actually happened.
            return JSONResponse({"error": scan_error_code(error)}, status_code=400)
        # The recorded choice comes from the intent (first record
        # wins end to end). A job that predates intents falls back
        # to the presented choice, gated by current availability.
        if intent is not None:
            choice = {"mode": intent["mode"], "email": intent["email"]}
        else:
            choice = {"mode": mode if notify_available(token) else "wait",
                      "email": getattr(token, "email", None)}
        try:
            await asyncio.to_thread(
                store.record_app_job, job["id"], owner=owner,
                operation="scan", notification=choice,
                **({"require_intent_id": intent["intent_id"]}
                   if intent is not None else {}))
        except ValueError:
            # The job the service returned is not the job this
            # intent created (a foreign job won the request-key
            # race): refuse rather than attribute it (round-5 F11).
            return JSONResponse({"error": "request_key already used"},
                                status_code=409)
        completion = await asyncio.to_thread(completion_view, job["id"])
        # The waiting screen's first paint: this job's own estimate
        # from its actual place in the lane.
        estimate = await asyncio.to_thread(
            estimate_sync, "scan", None, before_job_id=job["id"],
            owner=owner)
        return JSONResponse({"job_id": job["id"], "state": job["state"],
                             "operation": "scan", "cost": job["cost"],
                             "completion": completion,
                             "estimate": estimate})

    async def jobs_irradiate_prepare(request):
        """S5's server side: validate the scan and park the shown
        quote as a one-shot confirmation (§5.4). Nothing is charged
        or queued here."""
        if not enabled:
            return denied()
        owner, _token = await authenticate(request)
        if owner is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if service is None:
            return denied()
        body = await body_object(request)
        if body is None:
            return JSONResponse({"error": "JSON object required"}, status_code=400)
        scan_id = body.get("scan_id")
        if not isinstance(scan_id, str) or not scan_id:
            return JSONResponse({"error": "scan_id required"}, status_code=400)
        try:
            count = body.get("count")
            quote = irradiation_quote(count)
        except (ValueError, TypeError):
            return JSONResponse({"error": "invalid_count"}, status_code=400)
        attributed = await asyncio.to_thread(store.app_job, scan_id)
        if attributed is None or attributed["si_owner"] != owner \
                or attributed["operation"] != "scan":
            return JSONResponse({"error": "Scan not found"}, status_code=404)
        try:
            scan = await asyncio.to_thread(store.get, owner, scan_id)
        except KeyError:
            return JSONResponse({"error": "Scan not found"}, status_code=404)
        if scan["payload"].get("content_removed"):
            return JSONResponse({"error": "scan_expired"}, status_code=400)
        if scan["state"] != "completed" or scan["payload"].get("operation") != "scan":
            return JSONResponse({"error": "scan_not_completed"}, status_code=400)
        entries = (scan["result"] or {}).get("scan", {}).get("final_entries") \
            if isinstance(scan.get("result"), dict) else None
        if not entries:
            return JSONResponse({"error": "scan_has_no_entries"}, status_code=400)
        confirmation_id = await asyncio.to_thread(
            store.create_app_confirmation, owner, scan_id, quote,
            ttl_seconds=CONFIRMATION_TTL_SECONDS)
        balance = await asyncio.to_thread(balance_view, owner)
        available = balance.get("available")
        # The confirmation screen shows the duration estimate next
        # to the quote: same validated count, same lane basis as
        # the standalone estimate endpoint.
        estimate = await asyncio.to_thread(
            estimate_sync, "irradiation", count)
        return JSONResponse({"confirmation_id": confirmation_id,
                             "scan_id": scan_id, "quote": quote,
                             "available": available,
                             "balance_after": (available - quote["credits"]
                                               if type(available) is int else None),
                             "estimate": estimate,
                             "expires_at": _iso(time.time() + CONFIRMATION_TTL_SECONDS)})

    async def jobs_irradiate(request):
        if not enabled:
            return denied()
        owner, token = await authenticate(request)
        if owner is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if service is None:
            return denied()
        body = await body_object(request)
        if body is None:
            return JSONResponse({"error": "JSON object required"}, status_code=400)
        request_key = request_key_of(body)
        if request_key is None:
            return JSONResponse({"error": "request_key required"}, status_code=400)
        confirmation_id = body.get("confirmation_id")
        if not isinstance(confirmation_id, str) or not confirmation_id:
            return JSONResponse({"error": "confirmation_id required"}, status_code=400)
        try:
            mode = completion_choice(body)
        except ValueError:
            return JSONResponse({"error": "invalid_completion_mode"},
                                status_code=400)
        def consent_record(confirmation):
            return {"owner": owner, "subject": token.subject,
                    "client_id": token.client_id,
                    "confirmation_id": confirmation_id,
                    "consented_at": _iso(time.time()), "operation": "irradiation",
                    "count": confirmation["count"],
                    "credits_per_shot": confirmation["credits_per_shot"],
                    "total_credits": confirmation["total_credits"]}

        async def job_response(job):
            completion = await asyncio.to_thread(completion_view, job["id"])
            estimate = await asyncio.to_thread(
                estimate_sync, "irradiation",
                job["payload"].get("irradiation_count"),
                before_job_id=job["id"], owner=owner)
            return JSONResponse({"job_id": job["id"], "state": job["state"],
                                 "operation": "irradiation", "cost": job["cost"],
                                 "quote": job["payload"].get("quote"),
                                 "completion": completion,
                                 "estimate": estimate})

        confirmation = await asyncio.to_thread(store.app_confirmation, owner,
                                               confirmation_id)
        if confirmation is None:
            return JSONResponse({"error": "confirmation_not_found"}, status_code=400)
        # The durable choice for this request key (Astra round-4
        # F2): recorded before anything else can commit, first
        # record wins, and the WINNING row — not this request's
        # presentation — governs every path below, so a crashed
        # first attempt's original choice is what recovery uses.
        # The verified consent snapshot is persisted with it
        # (round-5 F2), so maintenance can finalize a crashed
        # attempt with exactly these records. Operation and
        # confirmation reference are immutable parts of the
        # binding (round-5 F13): a winner naming another operation
        # or confirmation refuses the key outright.
        intent, intent_created = await asyncio.to_thread(
            store.record_app_submission_intent, owner, request_key,
            operation="irradiation", mode=mode,
            email=getattr(token, "email", None),
            reference=confirmation_id,
            consent=consent_record(confirmation))
        if intent["operation"] != "irradiation" \
                or intent["reference"] != confirmation_id:
            return JSONResponse({"error": "request_key already used"},
                                status_code=409)
        choice = {"mode": intent["mode"], "email": intent["email"]}

        def effective_choice():
            """The choice as it may be recorded by a recovery path:
            an intent created by THIS request while delivery cannot
            honour notify downgrades to wait (the fresh path refuses
            instead); a pre-existing intent was recorded under a
            valid configuration and stands verbatim."""
            if intent_created and choice["mode"] == "notify" \
                    and not (notify_configured and choice["email"]):
                return {"mode": "wait", "email": choice["email"]}
            return choice
        # Request-key replay: the same confirmation already produced
        # this job; answer with it instead of consuming anything.
        existing = await asyncio.to_thread(store.get_by_request, owner, request_key)
        if existing is not None:
            attributed = await asyncio.to_thread(store.app_job, existing["id"])
            if attributed is not None:
                consent = attributed["consent"] or {}
                if attributed["operation"] == "irradiation" \
                        and consent.get("confirmation_id") == confirmation_id:
                    # Repair pass: if the original attempt committed
                    # attribution but lost the notification record
                    # (crash between the two writes), record the
                    # presented choice now. INSERT OR IGNORE makes
                    # this a no-op whenever the record exists, so the
                    # first choice still wins.
                    repair = effective_choice()
                    await asyncio.to_thread(
                        store.set_job_notification, existing["id"],
                        owner=owner, operation="irradiation",
                        mode=repair["mode"], email=repair["email"])
                    return await job_response(existing)
                return JSONResponse({"error": "request_key already used"},
                                    status_code=409)
            # The job exists but was never attributed: an earlier
            # attempt with this key stopped between submission and
            # attribution. Complete the attribution now when the
            # presented confirmation is the one that authorized this
            # exact job (same scan, same charged total, and the
            # confirmation still bound to that attempt — enforced
            # atomically by record_app_irradiation).
            if existing["payload"].get("operation") == "irradiation" \
                    and existing["payload"].get("scan_id") == confirmation["scan_id"] \
                    and existing["cost"] == confirmation["total_credits"]:
                # Recovery path: the job already exists and was
                # charged, so refusing here would strand it. The
                # durable choice is recorded atomically with the
                # attribution (effective form: an intent created by
                # this very request while delivery is unconfigured
                # records wait, and the response reports what was
                # recorded).
                repair = effective_choice()
                try:
                    await asyncio.to_thread(
                        store.record_app_irradiation, existing["id"],
                        owner=owner, consent=consent_record(confirmation),
                        confirmation_id=confirmation_id,
                        request_key=request_key,
                        notification=repair)
                except ValueError:
                    return JSONResponse({"error": "request_key already used"},
                                        status_code=409)
                return await job_response(existing)
            return JSONResponse({"error": "request_key already used"}, status_code=409)
        # The charge must equal the quote the user confirmed; a price
        # change since prepare returns the user to S5, never a
        # silently different charge.
        try:
            current = irradiation_quote(confirmation["count"])
        except (ValueError, TypeError):
            return JSONResponse({"error": "invalid_count"}, status_code=400)
        if (current["credits_per_shot_tenths"], current["credits"]) != \
                (confirmation["credits_per_shot_tenths"], confirmation["total_credits"]):
            return JSONResponse({"error": "quote_changed"}, status_code=400)
        if choice["mode"] == "notify" \
                and not (notify_configured and choice["email"]):
            # Fresh submissions only (replays above stay answerable):
            # refuse before the confirmation is claimed, so nothing
            # is consumed by a promise the service cannot keep. The
            # winning intent governs: its address was captured from a
            # verified token, so configuration is what matters here.
            # An intent created by this refused attempt is discarded
            # with it, so a later retry may present a different
            # choice; a pre-existing intent stands.
            if intent_created:
                await asyncio.to_thread(
                    store.discard_app_submission_intent, owner,
                    request_key, intent["intent_id"])
            return JSONResponse({"error": "notify_unavailable"}, status_code=400)
        # Claim the confirmation atomically BEFORE submitting
        # anything. Only the claim holder may create the job, so a
        # concurrent confirm with a different request key can never
        # also reserve credits; a retry with this key resumes the
        # claim and the request-key-idempotent submission returns
        # the one job.
        try:
            claim = await asyncio.to_thread(
                store.claim_app_confirmation, owner, confirmation_id, request_key)
        except ValueError as error:
            if str(error) == "Confirmation not found":
                return JSONResponse({"error": "confirmation_not_found"}, status_code=400)
            if str(error) == "Confirmation expired":
                return JSONResponse({"error": "confirmation_expired"}, status_code=400)
            return JSONResponse({"error": "confirmation_already_used"}, status_code=400)
        if claim["state"] == "used":
            # Finalized by an earlier attempt. Answer with its job
            # when this request key produced it (a concurrent twin
            # that slipped past the replay check above); otherwise
            # the confirmation is spent.
            winner = await asyncio.to_thread(store.get_by_request, owner, request_key)
            if winner is not None and winner["id"] == claim["job_id"]:
                return await job_response(winner)
            return JSONResponse({"error": "confirmation_already_used"}, status_code=400)
        try:
            job = await asyncio.to_thread(
                service.submit_irradiation, owner, request_key,
                confirmation["scan_id"], count=confirmation["count"],
                app_intent_id=intent["intent_id"],
                confirmation_id=confirmation_id)
        except KeyError:
            await asyncio.to_thread(store.release_app_confirmation_claim,
                                    owner, confirmation_id, request_key)
            return JSONResponse({"error": "Scan not found"}, status_code=404)
        except (ValueError, TypeError):
            await asyncio.to_thread(store.release_app_confirmation_claim,
                                    owner, confirmation_id, request_key)
            return JSONResponse({"error": "irradiation_rejected"}, status_code=400)
        try:
            await asyncio.to_thread(
                store.record_app_irradiation, job["id"], owner=owner,
                consent=consent_record(confirmation),
                confirmation_id=confirmation_id, request_key=request_key,
                notification=choice)
        except ValueError:
            return JSONResponse({"error": "request_key already used"},
                                status_code=409)
        return await job_response(job)

    # --- Native-app AI reading (native-reading-design-20261122) ---
    # The optional, credit-charged exception to the app's AI-free
    # default. Prepare parks the shown price as a one-shot
    # confirmation exactly like irradiation; create reserves the
    # charge, generates OUTSIDE any transaction, and settles only
    # with the validated text — any failure releases in full.

    def reading_gate_open():
        return enabled and reading_enabled and reading_service is not None

    def reading_view(reading):
        return {"reading_id": reading["id"], "scan_id": reading["scan_id"],
                "state": reading["state"], "credits": reading["credits"],
                "text": reading["text"], "text_expired": reading["text_expired"],
                "error_code": reading["error_code"],
                "created_at": _iso(reading["created_at"])}

    async def validated_scan(owner, scan_id):
        """The scan checks of irradiate/prepare, shared verbatim:
        (scan, None) or (None, error response)."""
        attributed = await asyncio.to_thread(store.app_job, scan_id)
        if attributed is None or attributed["si_owner"] != owner \
                or attributed["operation"] != "scan":
            return None, JSONResponse({"error": "Scan not found"}, status_code=404)
        try:
            scan = await asyncio.to_thread(store.get, owner, scan_id)
        except KeyError:
            return None, JSONResponse({"error": "Scan not found"}, status_code=404)
        if scan["payload"].get("content_removed"):
            return None, JSONResponse({"error": "scan_expired"}, status_code=400)
        if scan["state"] != "completed" or scan["payload"].get("operation") != "scan":
            return None, JSONResponse({"error": "scan_not_completed"}, status_code=400)
        entries = (scan["result"] or {}).get("scan", {}).get("final_entries") \
            if isinstance(scan.get("result"), dict) else None
        if not entries:
            return None, JSONResponse({"error": "scan_has_no_entries"}, status_code=400)
        return scan, None

    async def reading_prepare(request):
        """S4c's server side: validate the scan and park the shown
        reading price as a one-shot confirmation. Nothing is
        charged or generated here."""
        if not reading_gate_open():
            return denied()
        owner, _token = await authenticate(request)
        if owner is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        scan_id = request.path_params["job_id"]
        _scan, error = await validated_scan(owner, scan_id)
        if error is not None:
            return error
        quote = reading_quote()
        confirmation_id = await asyncio.to_thread(
            store.create_app_reading_confirmation, owner, scan_id, quote,
            ttl_seconds=CONFIRMATION_TTL_SECONDS)
        balance = await asyncio.to_thread(balance_view, owner)
        available = balance.get("available")
        return JSONResponse({"confirmation_id": confirmation_id,
                             "scan_id": scan_id, "quote": quote,
                             "available": available,
                             "balance_after": (available - quote["credits"]
                                               if type(available) is int else None),
                             "expires_at": _iso(time.time() + CONFIRMATION_TTL_SECONDS)})

    async def readings_create(request):
        if not reading_gate_open():
            return denied()
        owner, token = await authenticate(request)
        if owner is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        body = await body_object(request)
        if body is None:
            return JSONResponse({"error": "JSON object required"}, status_code=400)
        request_key = request_key_of(body)
        if request_key is None:
            return JSONResponse({"error": "request_key required"}, status_code=400)
        confirmation_id = body.get("confirmation_id")
        if not isinstance(confirmation_id, str) or not confirmation_id:
            return JSONResponse({"error": "confirmation_id required"}, status_code=400)

        async def replay_answer(existing):
            """The stored outcome as this endpoint's answer."""
            if existing["state"] == "completed":
                balance = await asyncio.to_thread(balance_view, owner)
                view = reading_view(existing)
                view["credits_charged"] = existing["credits"]
                view["balance_after"] = balance.get("available")
                return JSONResponse(view)
            if existing["state"] == "failed":
                # A stored TERMINAL failure is identified as such
                # (Astra round-4 N8): the client must be able to
                # tell "this purchase is finished and refunded —
                # clear the pending record" apart from a fresh
                # reading_unavailable refusal, which carries no
                # reading identity and may still heal under the
                # same request key.
                return JSONResponse(
                    {"error": existing["error_code"] or "reading_unavailable",
                     "reading_id": existing["id"], "state": "failed",
                     "terminal": True},
                    status_code=400)
            return JSONResponse(reading_view(existing))

        # Request-key replay: the stored outcome is the answer —
        # the provider is never called twice for one key, and no
        # second charge can occur (design §4.2).
        existing = await asyncio.to_thread(store.reading_by_request, owner, request_key)
        if existing is not None:
            # Input identity (batch 2b item 9): the stored outcome is
            # the answer only for the SAME request — the presented
            # confirmation must be the one that authorized the stored
            # reading. A key reused with another scan's confirmation
            # is a conflict, never a silent answer with the wrong
            # reading.
            if existing["confirmation_id"] != confirmation_id:
                return JSONResponse({"error": "request_key already used"},
                                    status_code=409)
            # Recovery (Astra round-9 F3): a state left by the
            # pre-fix implementation can pair this key's stored
            # outcome with an earlier attempt's landed-but-
            # rowless reservation. Fence and queue that orphan's
            # release before answering — the fence is local and
            # immediate; delivery belongs to maintenance, so the
            # replay never waits on D-Planet (Astra round-10 F3).
            # A no-op for every healthy key.
            await asyncio.to_thread(
                store.compensate_orphan_intent_for_key, owner, request_key)
            return await replay_answer(existing)
        confirmation = await asyncio.to_thread(store.app_reading_confirmation,
                                               owner, confirmation_id)
        if confirmation is None:
            return JSONResponse({"error": "confirmation_not_found"}, status_code=400)
        if confirmation["used_reading"] is not None:
            return JSONResponse({"error": "confirmation_already_used"}, status_code=400)
        if time.time() > confirmation["expires_at"]:
            return JSONResponse({"error": "confirmation_expired"}, status_code=400)
        current = reading_quote()
        if (confirmation["credits"], confirmation["price_version"]) != \
                (current["credits"], current["price_version"]):
            return JSONResponse({"error": "quote_changed"}, status_code=400)
        scan, error = await validated_scan(owner, confirmation["scan_id"])
        if error is not None:
            return error
        reading_id = str(uuid.uuid4())
        record = {"owner": owner, "subject": token.subject,
                  "client_id": token.client_id,
                  "confirmation_id": confirmation_id,
                  "consented_at": _iso(time.time()), "operation": "reading",
                  "credits": current["credits"],
                  "price_version": current["price_version"]}
        try:
            reserved = await asyncio.to_thread(
                store.reserve_reading, owner, reading_id, confirmation_id,
                request_key, credits=current["credits"],
                price_version=current["price_version"], consent=record)
        except ReadingChargeError as charge_error:
            if charge_error.code == "request_key_conflict":
                return JSONResponse({"error": "request_key already used"},
                                    status_code=409)
            if charge_error.code in ("reading_pending",
                                     "confirmation_already_used"):
                # reading_pending (Astra round-1 F3): the remote
                # reserve's outcome is ambiguous, but the reading row
                # exists in 'reserved' state — answer with the stored
                # row so the client polls its status under the same
                # request key instead of buying again with a new one.
                # confirmation_already_used (F6): a concurrent create
                # consumed the confirmation — the winner's stored
                # outcome is the answer, under the same identity
                # check as a replay.
                winner = await asyncio.to_thread(store.reading_by_request,
                                                 owner, request_key)
                if winner is None or winner["confirmation_id"] != confirmation_id:
                    return JSONResponse({"error": "request_key already used"},
                                        status_code=409)
                return await replay_answer(winner)
            return JSONResponse({"error": charge_error.code}, status_code=400)
        except KeyError:
            return JSONResponse({"error": "Account not found"}, status_code=404)
        except sqlite3.IntegrityError:
            # A concurrent create with this request key won the
            # insert (batch 2b item 9): answer with the stored
            # outcome after the same identity check as a replay,
            # never with an unhandled database error.
            winner = await asyncio.to_thread(store.reading_by_request,
                                             owner, request_key)
            if winner is None or winner["confirmation_id"] != confirmation_id:
                return JSONResponse({"error": "request_key already used"},
                                    status_code=409)
            return await replay_answer(winner)
        if not reserved.get("created", True):
            # The reservation already existed (Astra round-1 F6):
            # only the creating caller generates. Everyone else gets
            # the stored outcome — generating here could settle this
            # caller's scan text into the first caller's reading, or
            # release the first caller's reservation on failure.
            return await replay_answer(reserved)
        # The store may have adopted an earlier attempt's reading id
        # for this request key (durable reservation intent, batch 2b
        # item 1); settle/release always target the reserved row.
        reading_id = reserved["id"]
        try:
            # Async generation: the provider call is cancellable
            # at the service deadline in every network phase
            # (Astra round-4 F8), which a threaded sync call
            # cannot guarantee during connection setup.
            generated = await reading_service.generate_async(scan)
        except ReadingGenerationError as generation_error:
            await asyncio.to_thread(store.release_reading, owner, reading_id,
                                    error_code=generation_error.code,
                                    deliver=False)
            # This call created the reading and the failure is
            # terminal for it (released in full above): identify it
            # as terminal, like a replayed stored failure (N8).
            return JSONResponse({"error": generation_error.code,
                                 "reading_id": reading_id, "state": "failed",
                                 "terminal": True}, status_code=400)
        except Exception:
            # Any other generation failure still releases in full
            # before the error surfaces (design §4.1 step 5); the
            # orphan reconcile is only the backstop.
            await asyncio.to_thread(store.release_reading, owner, reading_id,
                                    error_code="reading_unavailable",
                                    deliver=False)
            raise
        # If the parent scan's content expired mid-generation, the
        # text is delivered on this response only and not retained
        # (its lifetime follows the scan's, design §3.5).
        try:
            fresh = await asyncio.to_thread(store.get, owner, confirmation["scan_id"])
            retain = not fresh["payload"].get("content_removed")
        except KeyError:
            retain = False
        settled = await asyncio.to_thread(
            store.settle_reading, owner, reading_id,
            text=generated["text"] if retain else None,
            model=generated.get("model"),
            prompt_tokens=generated.get("prompt_tokens"),
            completion_tokens=generated.get("completion_tokens"),
            provider_cost_microusd=generated.get("provider_cost_microusd"))
        balance = await asyncio.to_thread(balance_view, owner)
        view = reading_view(settled)
        view["text"] = generated["text"]
        view["credits_charged"] = settled["credits"]
        view["balance_after"] = balance.get("available")
        return JSONResponse(view)

    async def reading_get(request):
        if not reading_gate_open():
            return denied()
        owner, _token = await authenticate(request)
        if owner is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        reading = await asyncio.to_thread(store.get_reading, owner,
                                          request.path_params["reading_id"])
        if reading is None:
            return JSONResponse({"error": "Reading not found"}, status_code=404)
        return JSONResponse(reading_view(reading))

    async def job_status(request):
        if not enabled:
            return denied()
        owner, _token = await authenticate(request)
        if owner is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        job_id = request.path_params["job_id"]
        attributed = await asyncio.to_thread(store.app_job, job_id)
        if attributed is None or attributed["si_owner"] != owner:
            return JSONResponse({"error": "Job not found"}, status_code=404)
        try:
            view = await asyncio.to_thread(job_view, owner, job_id)
        except KeyError:
            return JSONResponse({"error": "Job not found"}, status_code=404)
        return JSONResponse(view)

    async def job_cancel(request):
        if not enabled:
            return denied()
        owner, _token = await authenticate(request)
        if owner is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        job_id = request.path_params["job_id"]
        attributed = await asyncio.to_thread(store.app_job, job_id)
        if attributed is None or attributed["si_owner"] != owner:
            return JSONResponse({"error": "Job not found"}, status_code=404)
        try:
            await asyncio.to_thread(store.cancel, owner, job_id)
        except KeyError:
            return JSONResponse({"error": "Job not found"}, status_code=404)
        except (ValueError, TypeError):
            return JSONResponse({"error": "cancel_rejected"}, status_code=400)
        view = await asyncio.to_thread(job_view, owner, job_id)
        return JSONResponse(view)

    async def job_viewed(request):
        # The explicit "I saw the result" ack (batch 2d,
        # MEDIUM 6). The status GET is side-effect free, so this
        # POST is the only thing that records a view — and it
        # records one only for a TERMINAL job: the view marker
        # suppresses the completion mail outright, so recording
        # it for an in-flight job (whose detail screen can be
        # opened from history) would cancel a mail still owed.
        # Idempotent: repeat acks answer the same.
        if not enabled:
            return denied()
        owner, _token = await authenticate(request)
        if owner is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        job_id = request.path_params["job_id"]
        attributed = await asyncio.to_thread(store.app_job, job_id)
        if attributed is None or attributed["si_owner"] != owner:
            return JSONResponse({"error": "Job not found"}, status_code=404)
        try:
            job = await asyncio.to_thread(store.get, owner, job_id)
            state = job["state"]
        except KeyError:
            return JSONResponse({"error": "Job not found"}, status_code=404)
        except (ValueError, AttributeError, TypeError, RecursionError):
            # Corrupt stored payload: the full read cannot decode
            # it, but the view record does not depend on the
            # payload — the columns carry the state without
            # decoding (the same fallback the status read uses
            # for its degraded view). A 500 here would strand the
            # ack for a job whose result the owner did see
            # (Antigravity glass audit, finding 4).
            try:
                columns = await asyncio.to_thread(
                    store.job_columns, owner, job_id)
            except KeyError:
                return JSONResponse({"error": "Job not found"},
                                    status_code=404)
            state = columns["state"]
        # 'unknown' is deliberately NOT an acknowledgeable
        # outcome (round 2): the owner who opens an unknown job
        # has seen uncertainty, not a result — the job may still
        # be resolved, and the resolved outcome is owed its own
        # notification. The poller's in-flight set agrees.
        if state not in ("completed", "failed", "cancelled"):
            completion = await asyncio.to_thread(completion_view, job_id)
            return JSONResponse({"viewed": False,
                                 "completion": completion})
        await asyncio.to_thread(store.mark_result_viewed, owner, job_id)
        completion = await asyncio.to_thread(completion_view, job_id)
        return JSONResponse({"viewed": True,
                             "completion": completion})

    async def history(request):
        if not enabled:
            return denied()
        owner, _token = await authenticate(request)
        if owner is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        rows = await asyncio.to_thread(store.recent_app_sessions, owner, limit=20)
        sessions = []
        for row in rows:
            if row["kind"] == "reading":
                sessions.append({"reading_id": row["reading_id"],
                                 "scan_id": row["scan_id"],
                                 "operation": "reading", "state": row["state"],
                                 "cost": row["cost"],
                                 "created_at": _iso(row["created_at"])})
            else:
                sessions.append({"job_id": row["job_id"],
                                 "operation": row["operation"],
                                 "state": row["state"], "cost": row["cost"],
                                 "targets": row["targets"],
                                 "intention": row["intention"],
                                 "self_love_joy": row["self_love_joy"],
                                 "created_at": _iso(row["created_at"])})
        return JSONResponse({"sessions": sessions})

    async def jobs_resume(request):
        """Redeem a completion-mail resume token. The token is a
        locator, not a credential: redemption requires the caller's
        own authenticated session and returns only which job to
        open. Unknown tokens answer 404 without revealing whether
        the token ever existed; the job's data stays behind the
        session exactly as everywhere else."""
        if not enabled:
            return denied()
        owner, _token = await authenticate(request)
        if owner is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        body = await body_object(request)
        if body is None:
            return JSONResponse({"error": "JSON object required"}, status_code=400)
        token_value = body.get("token")
        if not isinstance(token_value, str) \
                or not 16 <= len(token_value) <= 256:
            return JSONResponse({"error": "resume_invalid"}, status_code=400)
        try:
            redeemed = await asyncio.to_thread(
                store.redeem_resume_token, owner, token_value)
        except ValueError:
            return JSONResponse({"error": "resume_expired"}, status_code=400)
        if redeemed is None:
            return JSONResponse({"error": "resume_not_found"}, status_code=404)
        return JSONResponse(redeemed)

    # --- Referral (referral-system-design-20261122) ---
    # Gated on the referral switch alone (not the app rollout
    # switch): the /account page reuses these same endpoints with
    # the account token and hides its section on a 404.

    def referral_gate_open():
        return bool(referral_enabled)

    async def referral_get(request):
        if not referral_gate_open():
            return denied()
        owner, _token = await authenticate(request)
        if owner is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            status = await asyncio.to_thread(store.referral_status, owner)
        except KeyError:
            return JSONResponse({"error": "Account not found"}, status_code=404)
        return JSONResponse(status)

    async def referral_redeem(request):
        if not referral_gate_open():
            return denied()
        owner, _token = await authenticate(request)
        if owner is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        body = await body_object(request)
        code = body.get("code") if body is not None else None
        if not isinstance(code, str) or not code:
            return JSONResponse({"error": "invalid_code"}, status_code=400)
        try:
            result = await asyncio.to_thread(store.redeem_referral, owner, code)
        except KeyError:
            return JSONResponse({"error": "Account not found"}, status_code=404)
        except ReferralError as error:
            status_code = {"invalid_code": 400, "own_code": 400,
                           "already_redeemed": 409, "window_closed": 403,
                           "cross_unconfigured": 400,
                           "too_many_attempts": 429}.get(error.code, 400)
            return JSONResponse({"error": error.code}, status_code=status_code)
        balance = await asyncio.to_thread(balance_view, owner)
        result["available"] = balance.get("available")
        return JSONResponse(result)

    async def referral_gifts_seen(request):
        if not referral_gate_open():
            return denied()
        owner, _token = await authenticate(request)
        if owner is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        body = await body_object(request)
        ids = body.get("ids") if body is not None else None
        try:
            marked = await asyncio.to_thread(store.mark_referral_gifts_seen,
                                             owner, ids)
        except (ValueError, TypeError):
            return JSONResponse({"error": "ids required"}, status_code=400)
        return JSONResponse({"seen": marked})

    # JSON lives under /api/app/*; the HTML page is /app (web_ui), so
    # the two never collide.
    return [Route("/api/app/session", session, methods=["GET"]),
            Route("/api/app/referral", referral_get, methods=["GET"]),
            Route("/api/app/referral/redeem", referral_redeem, methods=["POST"]),
            Route("/api/app/referral/gifts/seen", referral_gifts_seen,
                  methods=["POST"]),
            Route("/api/app/targets", targets_list, methods=["GET"]),
            Route("/api/app/targets", targets_register, methods=["POST"]),
            Route("/api/app/targets/rename", targets_rename, methods=["POST"]),
            Route("/api/app/targets/delete", targets_delete, methods=["POST"]),
            Route("/api/app/estimate", job_estimate, methods=["GET"]),
            Route("/api/app/jobs/scan", jobs_scan, methods=["POST"]),
            Route("/api/app/jobs/irradiate/prepare", jobs_irradiate_prepare,
                  methods=["POST"]),
            Route("/api/app/jobs/irradiate", jobs_irradiate, methods=["POST"]),
            Route("/api/app/jobs/{job_id}/reading/prepare", reading_prepare,
                  methods=["POST"]),
            Route("/api/app/readings", readings_create, methods=["POST"]),
            Route("/api/app/readings/{reading_id}", reading_get, methods=["GET"]),
            Route("/api/app/jobs/{job_id}", job_status, methods=["GET"]),
            Route("/api/app/jobs/{job_id}/cancel", job_cancel, methods=["POST"]),
            Route("/api/app/jobs/{job_id}/viewed", job_viewed, methods=["POST"]),
            Route("/api/app/resume", jobs_resume, methods=["POST"]),
            Route("/api/app/history", history, methods=["GET"])]
