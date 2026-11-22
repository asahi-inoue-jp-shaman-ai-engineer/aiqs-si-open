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
import os
import time
import uuid
from datetime import datetime, timezone

from starlette.responses import JSONResponse
from starlette.routing import Route

from .auth import resolve_account
from .dplanet import DPlanetError
from .pricing import (CAMPAIGN_ACTIVE, CAMPAIGN_NAME, CREDITS_PER_SHOT,
                      PRICE_VERSION, READING_CREDITS, active_credits_per_shot,
                      active_rate_tenths, irradiation_quote, reading_quote)
from .reading import ReadingGenerationError
from .referral import ReferralError
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
               referral_enabled=False):
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

    def job_view(owner, job_id):
        """The app status payload for one of the owner's app jobs:
        the public serializer's job view plus the public proof
        bundle's display fields (execution/device/settlement/digest),
        mirroring the internal API's status shape minus player data."""
        job = store.get(owner, job_id)
        proof = store.execution_proof(owner, job_id)
        attributed = store.app_job(job_id)
        view = public_job_view(store, job)
        queue = store.queue_status(owner, job_id)
        payload = job["payload"]
        return {"job_id": job_id, "operation": view.get("operation"),
                "state": job["state"], "cost": job["cost"],
                "result": view.get("result"),
                # The user's own words, returned to themself only:
                # the §4.2 copy format heads the ranking with the
                # intention and self-love affirmation the user typed.
                "intention": payload.get("intention"),
                "self_love_joy": payload.get("self_love_joy"),
                "execution": proof["execution"], "device": proof["device"],
                "settlement": proof["settlement"],
                "proof_digest": proof["proof_digest"],
                "consent": attributed["consent"] if attributed else None,
                "queue": {"position": queue["position"],
                          "jobs_ahead": queue["jobs_ahead"]}}

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

    async def jobs_scan(request):
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
        existing = await asyncio.to_thread(store.get_by_request, owner, request_key)
        if existing is not None and (await asyncio.to_thread(
                store.app_job, existing["id"])) is None:
            # The key belongs to another channel; never re-attribute.
            return JSONResponse({"error": "request_key already used"}, status_code=409)
        try:
            job = await asyncio.to_thread(
                service.submit_scan, owner, request_key, body.get("intention"),
                body.get("target_ids"), self_love_joy=body.get("self_love_joy"))
        except KeyError:
            return JSONResponse({"error": "Target not found"}, status_code=404)
        except (ValueError, TypeError):
            return JSONResponse({"error": "scan_rejected"}, status_code=400)
        await asyncio.to_thread(store.record_app_job, job["id"], owner=owner,
                                operation="scan")
        return JSONResponse({"job_id": job["id"], "state": job["state"],
                             "operation": "scan", "cost": job["cost"]})

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
        return JSONResponse({"confirmation_id": confirmation_id,
                             "scan_id": scan_id, "quote": quote,
                             "available": available,
                             "balance_after": (available - quote["credits"]
                                               if type(available) is int else None),
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
        def consent_record(confirmation):
            return {"owner": owner, "subject": token.subject,
                    "client_id": token.client_id,
                    "confirmation_id": confirmation_id,
                    "consented_at": _iso(time.time()), "operation": "irradiation",
                    "count": confirmation["count"],
                    "credits_per_shot": confirmation["credits_per_shot"],
                    "total_credits": confirmation["total_credits"]}

        def job_response(job):
            return JSONResponse({"job_id": job["id"], "state": job["state"],
                                 "operation": "irradiation", "cost": job["cost"],
                                 "quote": job["payload"].get("quote")})

        confirmation = await asyncio.to_thread(store.app_confirmation, owner,
                                               confirmation_id)
        if confirmation is None:
            return JSONResponse({"error": "confirmation_not_found"}, status_code=400)
        # Request-key replay: the same confirmation already produced
        # this job; answer with it instead of consuming anything.
        existing = await asyncio.to_thread(store.get_by_request, owner, request_key)
        if existing is not None:
            attributed = await asyncio.to_thread(store.app_job, existing["id"])
            if attributed is not None:
                consent = attributed["consent"] or {}
                if attributed["operation"] == "irradiation" \
                        and consent.get("confirmation_id") == confirmation_id:
                    return job_response(existing)
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
                try:
                    await asyncio.to_thread(
                        store.record_app_irradiation, existing["id"], owner=owner,
                        consent=consent_record(confirmation),
                        confirmation_id=confirmation_id, request_key=request_key)
                except ValueError:
                    return JSONResponse({"error": "request_key already used"},
                                        status_code=409)
                return job_response(existing)
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
                return job_response(winner)
            return JSONResponse({"error": "confirmation_already_used"}, status_code=400)
        try:
            job = await asyncio.to_thread(
                service.submit_irradiation, owner, request_key,
                confirmation["scan_id"], count=confirmation["count"])
        except KeyError:
            await asyncio.to_thread(store.release_app_confirmation_claim,
                                    owner, confirmation_id, request_key)
            return JSONResponse({"error": "Scan not found"}, status_code=404)
        except (ValueError, TypeError):
            await asyncio.to_thread(store.release_app_confirmation_claim,
                                    owner, confirmation_id, request_key)
            return JSONResponse({"error": "irradiation_rejected"}, status_code=400)
        await asyncio.to_thread(store.record_app_irradiation, job["id"], owner=owner,
                                consent=consent_record(confirmation),
                                confirmation_id=confirmation_id,
                                request_key=request_key)
        return job_response(job)

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
        # Request-key replay: the stored outcome is the answer —
        # the provider is never called twice for one key, and no
        # second charge can occur (design §4.2).
        existing = await asyncio.to_thread(store.reading_by_request, owner, request_key)
        if existing is not None:
            if existing["state"] == "completed":
                balance = await asyncio.to_thread(balance_view, owner)
                view = reading_view(existing)
                view["credits_charged"] = existing["credits"]
                view["balance_after"] = balance.get("available")
                return JSONResponse(view)
            if existing["state"] == "failed":
                return JSONResponse(
                    {"error": existing["error_code"] or "reading_unavailable"},
                    status_code=400)
            return JSONResponse(reading_view(existing))
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
            await asyncio.to_thread(
                store.reserve_reading, owner, reading_id, confirmation_id,
                request_key, credits=current["credits"],
                price_version=current["price_version"], consent=record)
        except ReadingChargeError as charge_error:
            return JSONResponse({"error": charge_error.code}, status_code=400)
        except KeyError:
            return JSONResponse({"error": "Account not found"}, status_code=404)
        try:
            generated = await asyncio.to_thread(reading_service.generate, scan)
        except ReadingGenerationError as generation_error:
            await asyncio.to_thread(store.release_reading, owner, reading_id,
                                    error_code=generation_error.code)
            return JSONResponse({"error": generation_error.code}, status_code=400)
        except Exception:
            # Any other generation failure still releases in full
            # before the error surfaces (design §4.1 step 5); the
            # orphan reconcile is only the backstop.
            await asyncio.to_thread(store.release_reading, owner, reading_id,
                                    error_code="reading_unavailable")
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
                                 "created_at": _iso(row["created_at"])})
        return JSONResponse({"sessions": sessions})

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
            Route("/api/app/history", history, methods=["GET"])]
