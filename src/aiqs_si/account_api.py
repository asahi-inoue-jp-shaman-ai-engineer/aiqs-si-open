"""Authenticated browser account API: D-Planet linking + SI-only purchase.

The browser counterpart of the MCP link tools (design §2.1: the
auth-ui page is the secondary path next to the agent-driven one).
Both call the same flow implementation in aiqs_si.linking, so the
two-step verify -> both-names confirmation -> finalize + balance
transfer behaves identically whichever door the user takes.

Authentication is the server's user session: a Bearer access token
verified by the same verifier as the MCP and billing routes, resolved
to the account with resolve_account. The owner always comes from the
verified token — request bodies never name an account — so one user
cannot read or change another user's link state through these routes.

Responses are operation results in the same shape the MCP tools
return: HTTP 200 with the flow dictionary, including its "error"
field when an operation did not proceed. Only authentication
failures (401) and malformed bodies (400) use error statuses.

Purchase links (design §5.1) are Stripe Payment Links created by the
operator in the Stripe dashboard; this server only hands them out.
Each pack's URL is configured separately through the environment
(AIQS_SI_PAYMENT_LINK_<yen>); an unconfigured pack is reported with
url=None so the page can disable its button and say "準備中" instead
of linking nowhere. The per-user URL appends
client_reference_id=si:<owner_id>, the namespace the D-Planet webhook
split keys on (§5.2). Linked accounts get url=None for every pack:
their purchase path is D-Planet itself (§4.4).
"""
import asyncio
import os
from urllib.parse import quote, urlsplit

from starlette.responses import JSONResponse
from starlette.routing import Route

from . import linking
from .auth import resolve_account
from .pricing import PURCHASE_PACKS

PAYMENT_LINK_ENVS = {yen: f"AIQS_SI_PAYMENT_LINK_{yen}" for yen, _ in PURCHASE_PACKS}


def payment_links_from_env(environ=None):
    """Configured Stripe Payment Link URL per pack yen; None per unset pack.

    A configured value must be a plain HTTPS URL (no credentials, no
    fragment); anything else is an operator error and raises, so a
    typo fails startup instead of silently disabling a pack.
    """
    environ = os.environ if environ is None else environ
    links = {}
    for yen, name in PAYMENT_LINK_ENVS.items():
        value = environ.get(name)
        if value is None or (isinstance(value, str) and not value.strip()):
            links[yen] = None
            continue
        parsed = urlsplit(value) if isinstance(value, str) else None
        if (parsed is None or parsed.scheme != "https" or not parsed.hostname
                or parsed.username or parsed.password or parsed.fragment):
            raise ValueError(f"{name} must be a plain HTTPS Payment Link URL")
        links[yen] = value
    return links


def purchase_payload(store, owner, payment_links):
    """The purchase page's data for one account (design §5.1).

    The full per-user URL (with client_reference_id) is built here so
    the page never handles the raw owner id. While the account is
    linked, every pack reports url=None and "linked" tells the page
    to point the user at D-Planet instead.
    """
    linked = store.active_account_link(owner) is not None
    packs = []
    for yen, credits in PURCHASE_PACKS:
        url = None
        base = (payment_links or {}).get(yen)
        if not linked and base:
            separator = "&" if urlsplit(base).query else "?"
            url = base + separator + "client_reference_id=" + quote(f"si:{owner}", safe="")
        packs.append({"yen": yen, "credits": credits, "url": url})
    return {"linked": linked,
            "configured": any((payment_links or {}).get(yen) for yen, _ in PURCHASE_PACKS),
            "packs": packs}


def account_routes(store, verifier, *, dplanet_client, payment_links,
                   dplanet_routing=False):
    """User-authenticated account routes for the combined application."""

    async def authenticate(request):
        """The account owner from the verified Bearer token, or None."""
        header = request.headers.get("authorization", "")
        token = await verifier.verify_token(header[7:]) if header.startswith("Bearer ") else None
        if token is None:
            return None
        return resolve_account(store, token)

    async def body_object(request):
        try:
            body = await request.json()
        except ValueError:
            return None
        return body if isinstance(body, dict) else None

    def bounded(body, field, limit):
        value = body.get(field)
        return value if isinstance(value, str) and 1 <= len(value) <= limit else None

    async def link_status(request):
        owner = await authenticate(request)
        if owner is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        result = await asyncio.to_thread(
            linking.link_status, store, dplanet_client, owner, routing=dplanet_routing)
        return JSONResponse(result)

    async def link_verify(request):
        owner = await authenticate(request)
        if owner is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        body = await body_object(request)
        code = bounded(body, "code", 64) if body is not None else None
        if code is None:
            return JSONResponse({"error": "code required"}, status_code=400)
        result = await asyncio.to_thread(
            linking.verify_link_code, store, dplanet_client, owner, code)
        return JSONResponse(result)

    async def link_confirm(request):
        owner = await authenticate(request)
        if owner is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        body = await body_object(request)
        reservation_id = bounded(body, "reservation_id", 200) if body is not None else None
        if reservation_id is None:
            return JSONResponse({"error": "reservation_id required"}, status_code=400)
        result = await asyncio.to_thread(
            linking.confirm_link, store, dplanet_client, owner, reservation_id,
            routing=dplanet_routing)
        return JSONResponse(result)

    async def link_unlink(request):
        owner = await authenticate(request)
        if owner is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        result = await asyncio.to_thread(
            linking.unlink_account, store, dplanet_client, owner)
        return JSONResponse(result)

    async def purchase(request):
        owner = await authenticate(request)
        if owner is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        result = await asyncio.to_thread(purchase_payload, store, owner, payment_links)
        return JSONResponse(result)

    # JSON lives under /api/account/*; the HTML pages are /account/link
    # and /account/purchase (web_ui), so the two never collide.
    return [Route("/api/account/link", link_status, methods=["GET"]),
            Route("/api/account/link/verify", link_verify, methods=["POST"]),
            Route("/api/account/link/confirm", link_confirm, methods=["POST"]),
            Route("/api/account/link/unlink", link_unlink, methods=["POST"]),
            Route("/api/account/purchase", purchase, methods=["GET"])]
