"""Account MCP server factory (production composition).

The development-only MCP server (synthetic wiring checks) lives in
a private development package; this module never imports the
resonance engine or a device at module level.
"""
from mcp.server.mcpserver import MCPServer
from typing import Any
from pydantic import StrictInt
from contextlib import asynccontextmanager
import asyncio
import os
from .deployment import store_in_production
from .policies import enforce_policy_coherence, enforce_policy_posture
from .service import Service
from .maintenance import Maintenance
from .worker import Worker
from .store import Store
from .serializers import public_job_view
from . import linking
from .dplanet import DPlanetClient, DPlanetError, routing_enabled_from_env
from .pricing import (CAMPAIGN_ACTIVE, CAMPAIGN_CREDITS_PER_SHOT, CAMPAIGN_NAME,
                      CREDITS_PER_SHOT, PURCHASE_PACKS, active_credits_per_shot,
                      purchase_quote)

_DPLANET_FROM_ENV = object()

REGISTRATION_ENABLED_ENV = "AIQS_SI_REGISTRATION_ENABLED"


def registration_enabled_from_env(environ=None):
    """Operator switch reporting open registration; default OFF.

    Browser signup itself is opened on the identity provider's realm by
    the operator; this switch is flipped in the same change so that
    service_status and invitation_code never claim a registration path
    that is not actually open (and never hide one that is).
    """
    environ = os.environ if environ is None else environ
    value = environ.get(REGISTRATION_ENABLED_ENV, "")
    return isinstance(value, str) and value.strip().lower() in ("1", "true", "yes", "on")


def create_account_server(store, owner, *, embedded_test_worker=True, service=None, billing=None,
                          dplanet_client=_DPLANET_FROM_ENV, registration_ready=None, **server_options):
    """Local trusted-launcher identity only; not an HTTP authentication mechanism."""
    if store_in_production(store):
        # Policy posture at this composition boundary too
        # (Antigravity glass audit, finding 7): the account/
        # stdio MCP server had no gate, so a production-marked
        # process could start it on the built-in default
        # policies. First in the body: the default Service
        # construction below must never run ahead of the gate.
        enforce_policy_posture()
        # Policy coherence at this boundary too (batch 2g, R4):
        # posture validates the env, but the policies in effect
        # can still be stale snapshots — pricing froze at import
        # time, the store's grant/limit froze at construction.
        # create_application has compared them since batch 2e;
        # this direct constructor must not run production on the
        # stale values either (Astra batch2f diff review, R4).
        enforce_policy_coherence(store)
    if not callable(owner):
        store.balance(owner)
    def current_owner():
        return owner() if callable(owner) else owner
    service = service or Service(store,
                      {"synthetic-field": [{"id": "synthetic-1", "text": "Synthetic test entry"}]},
                      profile_salt=os.environ.get("AIQS_SI_PROFILE_SALT"))
    if service.store is not store:
        raise ValueError("MCP intake and account store must match")
    if billing is not None and billing.store is not store:
        raise ValueError('Billing and account store must match')
    if dplanet_client is _DPLANET_FROM_ENV:
        # Env-configured only; unset settings disable linking entirely.
        dplanet_client = DPlanetClient.from_env()
    # Consumption routing (design §4) stays off until L4 activation;
    # while off, linked accounts read and settle the SI balance as usual.
    dplanet_routing = routing_enabled_from_env()
    if registration_ready is None:
        registration_ready = registration_enabled_from_env()
    credit_kind = 'live' if billing is not None and billing.live else 'test'
    if service.mode == "hardware" and embedded_test_worker:
        raise ValueError("Hardware intake requires a separate worker")

    maintenance = Maintenance(store)

    @asynccontextmanager
    async def lifespan(server):
        # The embedded test worker's synthetic device lives in the
        # PC-only device package; it is imported lazily here so the
        # production import closure never reaches a device or the
        # resonance engine.
        executor = None
        if embedded_test_worker:
            from aiqs_si_device.synthetic import SyntheticDevice
            executor = Worker(store, SyntheticDevice())
        async def worker():
            while True:
                if executor is not None:
                    await asyncio.to_thread(executor.run_one)
                await asyncio.sleep(0.05)
        await maintenance.check()
        task = asyncio.create_task(worker())
        maintenance_task = asyncio.create_task(maintenance.run())
        try:
            yield {}
        finally:
            task.cancel()
            maintenance_task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            try:
                await maintenance_task
            except asyncio.CancelledError:
                pass

    rate_text = (
        f"Irradiation costs {CAMPAIGN_CREDITS_PER_SHOT} {credit_kind} credits per shot (1-99) "
        f"during the {CAMPAIGN_NAME} campaign (standard rate: {CREDITS_PER_SHOT} credits per shot); "
        "the campaign has no fixed end date and may end without notice. "
        if CAMPAIGN_ACTIVE else
        f"Irradiation costs {CREDITS_PER_SHOT} {credit_kind} credits per shot (1-99); ")

    server = MCPServer("Aiqs SI", lifespan=lifespan, instructions=(
        f"Configured execution mode: {service.mode}. Scan then irradiate the completed scan. "
        f"{rate_text}scans are free. "
        "Reuse request_key for identical retries only. Test mode never represents real payment. "
        "Before scan, obtain a target image, intention and self_love_joy chosen by the user. "
        "Ask the user to fill 〇〇 in 私はこの意図、この祈りを通して、〇〇という魂の喜びを受け取ります。 "
        "Never invent or default the user's self_love_joy. Irradiation inherits these exact scan inputs. "
        "Supply images with each scan; no target registration is needed. "
        "After scan, show the scan result, shot count and credit cost and obtain the user's explicit approval each time before irradiate. "
        "Hardware mode queues real device work on a dedicated PC. Configured mode is not proof of connection or physical effects. "
        "For D-Planet linking, the user issues a link code in D-Planet and gives it to you: call link_account, "
        "show the user BOTH account names it returns, and call link_account_confirm only after the user explicitly confirms that pairing. "
        "Confirming a link moves the account's SI balance into the D-Planet wallet; report the balance_transfer amount and status from the result. "
        "Registration itself happens in the user's browser; validate_invitation_code and registration_status cover the parts an agent can check. "
        "execution_proof returns the server's verifiable execution record for a job when the user asks whether the device really ran."), **server_options)

    server.maintenance = maintenance

    @server.tool()
    def service_status() -> dict[str, Any]:
        """Read actual capabilities and development prices."""
        return {"maintenance_healthy": maintenance.healthy, "mode": service.mode, "intake_kind": getattr(service, "intake_kind", None), "hardware_connected": None if service.mode == "hardware" else False, "registration_ready": registration_ready,
                "credits_enabled": True, "credit_kind": credit_kind, "scan_cost": 0,
                "irradiation_credits_per_shot": active_credits_per_shot(),
                "irradiation_standard_credits_per_shot": CREDITS_PER_SHOT,
                "campaign": ({"name": CAMPAIGN_NAME,
                              "credits_per_shot": CAMPAIGN_CREDITS_PER_SHOT,
                              "ends_without_notice": True} if CAMPAIGN_ACTIVE else None),
                "irradiation_count_min": 1,
                "irradiation_count_max": 99, "provisional_yen_per_credit": 1,
                "scan_price_pending_confirmation": False,
                "purchase_packs": [purchase_quote(yen) for yen, _ in PURCHASE_PACKS],
                "payments_enabled": billing is not None,
                "payment_mode": ('live' if billing.live else 'test') if billing is not None else None,
                "dplanet_link": {"configured": dplanet_client is not None,
                                 "linked": store.active_account_link(current_owner()) is not None},
                "real_irradiation_enabled": service.mode == "hardware"}

    @server.tool()
    def credit_balance() -> dict[str, Any]:
        """Read this account's available credits.

        While D-Planet routing is active and this account is linked,
        the balance lives in the D-Planet wallet and is read from there;
        if D-Planet cannot be reached the call answers with an error
        instead of silently substituting the SI-side figure (§4.1).
        Unlinked accounts always read their own SI balance.
        """
        identity = current_owner()
        link = store.active_account_link(identity)
        if dplanet_routing and link is not None:
            if dplanet_client is None:
                return {"error": "D-Planet balance routing is enabled but not configured",
                        "kind": credit_kind, "dplanet_linked": True,
                        "balance_source": "dplanet"}
            try:
                remote = dplanet_client.balance(link["dplanet_player_id"])
            except DPlanetError as error:
                return {"error": error.message, "http_status": error.status,
                        "kind": credit_kind, "dplanet_linked": True,
                        "balance_source": "dplanet"}
            return {"available": remote.get("balance"), "kind": credit_kind,
                    "dplanet_linked": True, "balance_source": "dplanet"}
        return {"available": store.balance(identity), "kind": credit_kind,
                "dplanet_linked": link is not None}

    @server.tool()
    def invitation_code() -> dict[str, Any]:
        """Get your reusable invitation code. Rewards require verified referred registration; this call grants nothing."""
        return {"code": store.invitation_code(current_owner()),
                "inviter_credits": store.grant_policy.referral_credits, "invitee_credits": store.grant_policy.referral_credits,
                "registration_ready": registration_ready, "kind": credit_kind}

    @server.tool()
    def regenerate_invitation_code() -> dict[str, Any]:
        """Replace your invitation code with a fresh one.

        The previous code stops working immediately for new
        registrations; accounts that already registered through it
        are unaffected. Use this when a code was shared too widely.
        """
        return {"code": store.regenerate_invitation_code(current_owner()),
                "inviter_credits": store.grant_policy.referral_credits,
                "invitee_credits": store.grant_policy.referral_credits,
                "registration_ready": registration_ready, "kind": credit_kind}

    @server.tool()
    def validate_invitation_code(code: str) -> dict[str, Any]:
        """Check an invitation code before browser registration starts.

        Validity only; the inviter is never identified, and a code is not
        usable by its own owner. Registration itself (signup form, email
        confirmation, consent) happens in the user's browser — this check
        is the part an agent can carry before handing over to it.
        """
        return store.invitation_code_status(current_owner(), code)

    @server.tool()
    def registration_status() -> dict[str, Any]:
        """How this account was registered and whether a referral attached.

        Referral codes cannot be added after registration, so this is
        final. An agent can confirm completion with this after the user
        finishes the browser signup and first connection.
        """
        return store.registration_status(current_owner())

    @server.tool()
    def link_status() -> dict[str, Any]:
        """Read this account's D-Planet link state.

        Linking is optional: an unlinked account keeps its own SI balance
        as its real balance. credit_routing reports where consumption
        settles: "dplanet" only while routing is activated (L4) and this
        account is linked, otherwise "si_local".
        """
        return linking.link_status(store, dplanet_client, current_owner(),
                                   routing=dplanet_routing)

    @server.tool()
    def link_account(code: str) -> dict[str, Any]:
        """Step 1 of D-Planet linking: verify a link code issued in D-Planet.

        Returns both accounts' display names plus a reservation. Show the
        names to the user; call link_account_confirm with the reservation
        only after the user explicitly confirms this exact pairing.
        Verification alone never completes the link.
        """
        return linking.verify_link_code(store, dplanet_client, current_owner(), code)

    @server.tool()
    def link_account_confirm(reservation_id: str) -> dict[str, Any]:
        """Step 2: finalize a verified link after the user's explicit confirmation.

        Confirming also moves this account's whole SI balance into the
        D-Planet wallet (balance_transfer in the result). If D-Planet
        cannot be reached for the move, the link still stands and the
        transfer stays queued for retry — it is never duplicated and
        never silently dropped; tell the user its status either way.
        """
        return linking.confirm_link(store, dplanet_client, current_owner(),
                                    reservation_id, routing=dplanet_routing)

    @server.tool()
    def unlink_account() -> dict[str, Any]:
        """Unlink this account from D-Planet (either side may initiate).

        Blocked while a job is running or unresolved. Credits already in
        the D-Planet wallet stay there — there is no reverse transfer —
        and this account's own SI balance is not changed by unlinking.
        """
        return linking.unlink_account(store, dplanet_client, current_owner())

    if billing is not None:
        @server.tool()
        def purchase_credits(request_key: str, yen: StrictInt) -> dict[str, Any]:
            """Create a manual Stripe Checkout link for an approved pack. User pays; this never starts irradiation."""
            return billing.checkout(current_owner(), request_key, yen)

        @server.tool()
        def purchase_status(order_id: str) -> dict[str, Any]:
            """Check this account's purchase receipt and balance. Checkout return alone does not prove payment."""
            return billing.purchase_status(current_owner(), order_id)

    @server.tool()
    def scan(request_key: str, intention: str, images: list[str], self_love_joy: str) -> dict[str, Any]:
        """Scan request images (base64 bytes, not URLs). Intention is recorded, not mixed into selection.

        When a scan completes, presenting the resonance ranking to the user
        is mandatory: show the top entries (field, content, hits) in
        descending hit order; never finish with only a status or a count.
        After the ranking, always offer choices — three are mandatory:
        "Proceed to irradiation as-is", "Read the subconscious from the
        ranking", and "Acknowledge the ranking (stop here)"; add, when
        fitting, choosing a shot count and irradiating, or changing the
        intention and re-scanning. On chat surfaces present them as tappable
        options. Choosing to proceed is not itself approval to irradiate:
        before executing, still show the shot count and credit cost and
        obtain explicit consent, unless the count and amount were already
        fixed in the user's original instruction.

        The reading option rests on this basis: the morphic-field resonance
        database's keywords resonate with the user's subconscious. A reading
        interprets three vectors together — the ranking's keyword set, the
        intention prompt, and the self-love affirmation — and is presented
        as a reading session. When a reading finishes, always close with two
        steps: offer "Perform wave adjustment" — irradiating that scan,
        under the same shot-count, cost and explicit-consent rules — and ask
        "Is there anything further you would like read?". If the user names
        a new theme, loop back: new intention, re-scan, present the ranking,
        present the choices, and read again.
        """
        return public_job_view(store, service.submit_image_scan(current_owner(), request_key, intention, images, self_love_joy=self_love_joy))

    @server.tool()
    def irradiate(request_key: str, scan_id: str, count: StrictInt) -> dict[str, Any]:
        """Queue 1–99 shots: weak 1–33, medium 34–66, strong 67–99. Costs count credits.

        When an irradiation completes, fetch its execution_proof before
        reporting completion, then offer the closing choices: "Check the
        proof log" (show the proof's state, completed shot count,
        settlement, and proof_digest), "Set up the next irradiation" (agree
        the next intention, count, and timing — a setup is not an execution;
        at execution time show the count and cost and obtain explicit
        consent as usual), and finishing. At the end of the session, always
        re-read and show the current credit balance.
        """
        return public_job_view(store, service.submit_irradiation(current_owner(), request_key, scan_id, count=count))

    @server.tool()
    def job_result(job_id: str) -> dict[str, Any]:
        """Get this account's job status and result.

        When this returns a completed scan, presenting the resonance ranking
        to the user is mandatory: show the top entries (field, content,
        hits) in descending hit order; never finish with only a status or a
        count. After the ranking, always offer choices — three are
        mandatory: "Proceed to irradiation as-is", "Read the subconscious
        from the ranking", and "Acknowledge the ranking (stop here)"; add,
        when fitting, choosing a shot count and irradiating, or changing the
        intention and re-scanning. On chat surfaces present them as tappable
        options. Choosing to proceed is not itself approval to irradiate:
        before executing, still show the shot count and credit cost and
        obtain explicit consent, unless the count and amount were already
        fixed in the user's original instruction.

        The reading option rests on this basis: the morphic-field resonance
        database's keywords resonate with the user's subconscious. A reading
        interprets three vectors together — the ranking's keyword set, the
        intention prompt, and the self-love affirmation — and is presented
        as a reading session. When a reading finishes, always close with two
        steps: offer "Perform wave adjustment" — irradiating that scan,
        under the same shot-count, cost and explicit-consent rules — and ask
        "Is there anything further you would like read?". If the user names
        a new theme, loop back: new intention, re-scan, present the ranking,
        present the choices, and read again.

        When this returns a completed irradiation, fetch its
        execution_proof before reporting completion, then offer the closing
        choices: "Check the proof log" (show the proof's state, completed
        shot count, settlement, and proof_digest), "Set up the next
        irradiation" (agree the next intention, count, and timing — a setup
        is not an execution; at execution time show the count and cost and
        obtain explicit consent as usual), and finishing. At the end of the
        session, always re-read and show the current credit balance.
        """
        return public_job_view(store, store.get(current_owner(), job_id))

    @server.tool()
    def execution_proof(job_id: str) -> dict[str, Any]:
        """Read the server's verifiable execution record for your own job.

        Distinct from job_result: this returns the evidence chain — the
        request digest, the server-tracked shot progress, the
        completion state, the device’s scoped public id, and the
        settlement ledger — plus a proof_digest over that public
        bundle. It never claims physical output, which the server
        cannot verify, and other accounts' jobs are invisible.

        When an irradiation completes, fetch this proof before reporting
        completion; for the "Check the proof log" closing choice, show its
        essentials: state, completed shot count, settlement, and
        proof_digest. Then offer "Set up the next irradiation" (agree the
        next intention, count, and timing — a setup is not an execution; at
        execution time the usual count, cost and explicit-consent rules
        apply) and finishing. At the end of the session, always re-read and
        show the current credit balance.
        """
        return store.execution_proof(current_owner(), job_id)

    @server.tool()
    def queue_status(job_id: str) -> dict[str, Any]:
        """Read your job's FIFO position. Counts include scans and irradiation; no guaranteed start time."""
        return store.queue_status(current_owner(), job_id)

    @server.tool()
    def cancel_job(job_id: str) -> dict[str, Any]:
        """Cancel queued work, or request irradiation stop at the next shot boundary. Running means stop is not yet confirmed."""
        identity = current_owner()
        store.cancel(identity, job_id)
        return public_job_view(store, store.get(identity, job_id))

    return server


if __name__ == "__main__":
    if os.environ.get("AIQS_SI_DB"):
        create_account_server(Store(os.environ["AIQS_SI_DB"],
                                    dplanet_client=DPlanetClient.from_env(),
                                    dplanet_routing_enabled=routing_enabled_from_env()),
                              os.environ["AIQS_SI_OWNER"],
                              embedded_test_worker=os.environ.get("AIQS_SI_SEPARATE_WORKER") != "1").run(transport="stdio")
    else:
        raise SystemExit("AIQS_SI_DB is required to run the account server")
