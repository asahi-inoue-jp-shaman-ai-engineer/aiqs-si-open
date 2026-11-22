"""D-Planet link flow shared by the MCP tools and the account HTTP API.

One implementation of the two-step flow (design §2.3): verify parks a
reservation bound to the SI account, the user confirms the pairing
after seeing BOTH account names, and only then does confirm finalize
the link and move the SI balance into the D-Planet wallet (§4.4).
Every caller (an MCP tool acting for the user's agent, or an
authenticated browser request) resolves the account owner first; the
functions here never see credentials and never act for anyone but the
given owner.
"""
from .dplanet import DPlanetError


def link_unconfigured():
    return {"configured": False, "linked": False,
            "error": "D-Planet linking is not configured on this server yet"}


def link_status(store, client, owner, *, routing):
    """The owner's link state. credit_routing reports where consumption
    settles: "dplanet" only while routing is activated (L4) and this
    account is linked, otherwise "si_local"."""
    link = store.account_link(owner)
    active = link if link is not None and link["status"] == "active" else None
    return {"configured": client is not None,
            "linked": active is not None,
            "link": ({"dplanet_player_id": active["dplanet_player_id"],
                      "link_id": active["link_id"],
                      "linked_at": active["linked_at"]} if active else None),
            "credit_routing": "dplanet" if (routing and active is not None)
                              else "si_local"}


def verify_link_code(store, client, owner, code):
    """Step 1: verify a link code issued in D-Planet.

    Returns both accounts' display names plus a reservation. The
    caller must show the names to the user and call confirm_link with
    the reservation only after the user explicitly confirms this exact
    pairing. Verification alone never completes the link.
    """
    if client is None:
        return link_unconfigured()
    active = store.active_account_link(owner)
    if active is not None:
        return {"configured": True, "linked": True, "status": "already_linked",
                "dplanet_player_id": active["dplanet_player_id"],
                "link_id": active["link_id"]}
    try:
        verified = client.verify_link_code(code, owner)
    except DPlanetError as error:
        return {"configured": True, "linked": False,
                "error": error.message, "http_status": error.status}
    return {"configured": True, "linked": False, "status": "confirmation_required",
            "dplanet_player_id": verified.get("player_id"),
            "dplanet_player_name": verified.get("player_name"),
            "si_display_name": store.account_display_name(owner),
            "reservation_id": verified.get("reservation_id"),
            "expires_at": verified.get("expires_at"),
            "next_step": "Show the user both account names; call link_account_confirm "
                         "with reservation_id only after explicit confirmation."}


def confirm_link(store, client, owner, reservation_id, *, routing):
    """Step 2: finalize a verified link after explicit confirmation.

    Confirming also moves this account's whole SI balance into the
    D-Planet wallet (balance_transfer in the result). If D-Planet
    cannot be reached for the move, the link still stands and the
    transfer stays queued for retry — it is never duplicated and
    never silently dropped; the result reports its status either way.
    """
    if client is None:
        return link_unconfigured()
    try:
        finalized = client.finalize_link(reservation_id, owner)
    except DPlanetError as error:
        return {"configured": True, "linked": False,
                "error": error.message, "http_status": error.status}
    try:
        link = store.record_account_link(owner, finalized["player_id"], finalized["link_id"])
    except (ValueError, KeyError) as error:
        return {"configured": True, "linked": False, "error": str(error)}
    # Link-time referral reconciliation (fix round F-SI-3): if this
    # player already redeemed a referral on the D-Planet side,
    # record the SI-side claim row so a later SI redeem answers
    # already_redeemed instead of double-granting. Best-effort by
    # design: an unreachable D-Planet must never break linking.
    try:
        status_fn = getattr(client, "referral_redemption_status", None)
        if status_fn is not None:
            status = status_fn(finalized["player_id"])
            if isinstance(status, dict) and status.get("redeemed"):
                store.record_dplanet_cross_claim(owner)
    except Exception:
        pass
    transfer = store.transfer_balance_to_dplanet(owner, client)
    return {"configured": True, "linked": True, "status": "linked",
            "dplanet_player_id": link["dplanet_player_id"], "link_id": link["link_id"],
            "balance_transfer": {"amount": transfer["amount"],
                                 "status": transfer["status"]},
            "credit_routing": "dplanet" if routing else "si_local_until_activation"}


def unlink_account(store, client, owner):
    """Unlink the owner's account from D-Planet (either side may initiate).

    Blocked while a job is running or unresolved. Credits already in
    the D-Planet wallet stay there — there is no reverse transfer —
    and this account's own SI balance is not changed by unlinking.
    """
    active = store.active_account_link(owner)
    if active is None:
        return {"configured": client is not None, "linked": False,
                "status": "not_linked"}
    if store.has_unfinished_execution(owner):
        return {"configured": True, "linked": True, "status": "blocked",
                "error": "A running or unresolved job exists; unlink after it finishes"}
    if client is None:
        return {"configured": False, "linked": True, "status": "blocked",
                "error": "D-Planet linking is not configured; the unlink cannot be reflected"}
    try:
        client.unlink(si_owner=owner, link_id=active["link_id"],
                      player_id=active["dplanet_player_id"])
    except DPlanetError as error:
        return {"configured": True, "linked": True,
                "error": error.message, "http_status": error.status}
    store.mark_account_unlinked(owner)
    return {"configured": True, "linked": False, "status": "unlinked",
            "link_id": active["link_id"]}
