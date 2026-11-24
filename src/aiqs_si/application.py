"""One HTTP application with distinct user and dedicated-worker authorization."""
from aiqs_si.auth import create_mcp_http_app
from contextlib import asynccontextmanager
from starlette.applications import Starlette
from starlette.routing import Mount, Route
from starlette.responses import JSONResponse
from starlette.middleware import Middleware
from .request_limits import RequestBodyLimit, worker_boundary_key_from_env
from .auth import create_http_server
from .deployment import production_environment, store_in_production
from .policies import enforce_policy_posture
from .worker_api import create_worker_app
from starlette.datastructures import MutableHeaders


class _OnboardingDispatch:
    def __init__(self, app, *, onboarding):
        self.app, self.onboarding = app, onboarding

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'http' and (scope['path'] == '/onboarding' or scope['path'].startswith('/onboarding/')):
            if scope['path'] not in ('/onboarding/creation', '/onboarding/complete'):
                return await JSONResponse({'error': 'not_found'}, status_code=404)(scope, receive, send)
            return await self.onboarding(scope, receive, send)
        return await self.app(scope, receive, send)


class _OnboardingNoStore:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or not (scope['path'] == '/onboarding' or scope['path'].startswith('/onboarding/')):
            return await self.app(scope, receive, send)
        async def no_store(message):
            if message['type'] == 'http.response.start':
                headers = MutableHeaders(scope=message)
                headers['Cache-Control'] = 'no-store'
                headers['Pragma'] = 'no-cache'
            await send(message)
        await self.app(scope, receive, no_store)


def _enforce_policy_coherence(store):
    """The policies IN EFFECT must be the policies the deployment
    environment NOW describes (batch 2e, R4). Two snapshots can
    silently diverge from a fresh env load: the pricing module
    froze its policy at import time, and the store froze its
    grant/limit policies at construction. A host that imports or
    constructs before the service env is fully applied would
    then run production on stale money/exposure values while the
    env — and the posture check — say otherwise. Field-wise
    comparison against fresh loads; any divergence refuses the
    composition. Policy values themselves are never reported."""
    from . import pricing
    from .policies import (load_grant_policy, load_limit_policy,
                           load_pricing_policy)
    held_pairs = (
        ("pricing", pricing._POLICY, load_pricing_policy()),
        ("grant", store.grant_policy, load_grant_policy()),
        ("limit", store.limit_policy, load_limit_policy()),
    )
    for name, held, fresh in held_pairs:
        if vars(held) != vars(fresh):
            raise ValueError(
                f"the {name} policy in effect diverges from the "
                "deployment environment; refusing to start a "
                "production composition on stale policy values")


def create_application(store, user_verifier, worker_verifier, *, allowed_workers, service=None, billing=None, ui_directory=None,
                       onboarding_config=None, onboarding_binding=None, onboarding_hook_secret=None,
                       internal_link_key=None, dplanet_client=None, payment_links=None,
                       wellknown_transport=None, reading_provider=None,
                       notification_mailer=None, estimate_policy=None):
    allowed_workers = frozenset(allowed_workers)
    if user_verifier.audience == worker_verifier.audience:
        raise ValueError("User and worker audiences must be distinct")
    # Policy posture at the composition boundary (batch 2d,
    # MEDIUM 4): the CLI gate alone left every other composition
    # — ASGI assembled in code, tests promoted to production,
    # embedded MCP — free to run on the built-in default
    # policies silently. A production signal (the deployment
    # marker, or the production issuer) now requires the same
    # explicit posture here.
    production = (store_in_production(store, url=user_verifier.issuer)
                  or production_environment(url=worker_verifier.issuer))
    if production:
        enforce_policy_posture()
        # Worker fairness is a guarantee, not an option (batch
        # 2e, R3): the reserved verification channel exists only
        # when the operator configures the boundary credential —
        # without it, forged worker tokens spend the shared
        # verification budget and a rotating worker's first
        # verification starves behind the flood. A production
        # worker composition therefore REQUIRES the credential;
        # the worker transport presents it on every queue
        # request (worker_remote.RemoteQueue).
        if worker_boundary_key_from_env() is None:
            raise ValueError(
                "AIQS_SI_WORKER_BOUNDARY_KEY must be configured for a "
                "production worker composition")
        _enforce_policy_coherence(store)
    if billing is not None and billing.store is not store:
        raise ValueError('Billing and account store must match')
    onboarding = None
    if onboarding_config is not None:
        from .onboarding_app import create_onboarding_app
        if (user_verifier.issuer != onboarding_config.authority_issuer
                or user_verifier.audience != onboarding_config.resource_audience
                or user_verifier.broker_identity_issuer != onboarding_config.upstream_issuer
                or getattr(user_verifier, 'mapped_identity', False)
                or user_verifier.identity_issuer not in (None, onboarding_config.upstream_issuer)):
            raise ValueError('Onboarding verifier configuration mismatch')
        onboarding = create_onboarding_app(config=onboarding_config, existing_binding=onboarding_binding,
            store=store, hook_secret=onboarding_hook_secret)
    elif onboarding_binding is not None or onboarding_hook_secret is not None:
        raise ValueError('Onboarding configuration required')
    mcp = create_http_server(store, user_verifier, service=service, embedded_test_worker=False, billing=billing)
    mcp_app = create_mcp_http_app(mcp)
    workers = create_worker_app(store, worker_verifier, allowed_subjects=allowed_workers)
    # D-Planet -> SI internal receivers; while internal_link_key is
    # unset the app answers 404 (disabled), like D-Planet's key file.
    from .app_api import referral_enabled_from_env
    from .internal_api import create_internal_app
    internal = create_internal_app(store, internal_link_key, service=service,
                                   referral_enabled=referral_enabled_from_env())

    @asynccontextmanager
    async def lifespan(app):
        async with mcp_app.router.lifespan_context(mcp_app):
            yield

    async def health(request):
        healthy = mcp.maintenance.healthy
        return JSONResponse({'healthy': healthy}, status_code=200 if healthy else 503)

    billing_routes = billing.routes(user_verifier) if billing is not None else []
    # Browser account API (linking + SI-only purchase). The D-Planet
    # client and Payment Link URLs are injected by the host; while the
    # client is None the link routes answer "not configured", and
    # unconfigured packs report no URL — the safe pre-launch state.
    from .account_api import account_routes
    from .dplanet import routing_enabled_from_env
    account = account_routes(store, user_verifier, dplanet_client=dplanet_client,
                             payment_links=payment_links,
                             dplanet_routing=routing_enabled_from_env())
    # Standalone app API (si-standalone-app-design-20261122): the
    # user-session sibling of the internal jobs API. Gated by the
    # AIQS_SI_APP_ENABLED rollout switch; while off, its routes
    # answer 404 and the /app page is not served.
    from .app_api import app_enabled_from_env, app_routes, referral_enabled_from_env
    app_enabled = app_enabled_from_env()
    # Native-app AI reading (native-reading-design-20261122): the
    # service exists only while its flag is ON and a provider is
    # available (injected here in tests, built from the env key in
    # production); otherwise the reading routes answer 404.
    from .reading import build_reading_service
    reading_service, reading_enabled = build_reading_service(
        store, provider=reading_provider)
    # Completion notifications (job-notify-resume design): the
    # delivery is built from the environment (Resend key) or the
    # injected mailer (tests). Unconfigured, the store keeps the
    # fail-soft None and the app never offers the notify choice.
    from .notifications import build_notification_delivery
    notification_delivery = build_notification_delivery(
        store, mailer=notification_mailer)
    store.notification_delivery = notification_delivery
    # Duration estimates (foundation item 3): the policy is loaded
    # from the env by the estimates module; tests inject a measured
    # policy explicitly. Unset seconds values disable the estimate
    # per operation — the API answers null rather than a guess.
    standalone = app_routes(store, user_verifier, service=service,
                            dplanet_client=dplanet_client,
                            dplanet_routing=routing_enabled_from_env(),
                            enabled=app_enabled,
                            reading_service=reading_service,
                            reading_enabled=reading_enabled,
                            referral_enabled=referral_enabled_from_env(),
                            notify_configured=notification_delivery is not None,
                            estimate_policy=estimate_policy)
    from .web_ui import ui_routes
    public_routes = ui_routes(ui_directory, user_verifier.issuer,
                              app_enabled=app_enabled) if ui_directory else []
    middleware = [Middleware(RequestBodyLimit, worker_verifier=worker_verifier, allowed_workers=allowed_workers,
                             account_verifier=user_verifier,
                             worker_boundary_key=worker_boundary_key_from_env())]
    if onboarding is not None:
        middleware.append(Middleware(_OnboardingDispatch, onboarding=onboarding))
    from .wellknown import wellknown_routes
    discovery = wellknown_routes(user_verifier, transport=wellknown_transport)
    app = Starlette(routes=[Route("/health", health), *workers.routes, *internal.routes, *billing_routes, *account, *standalone, *public_routes, *discovery, Mount("/", app=mcp_app)],
                    lifespan=lifespan, middleware=middleware)
    return _OnboardingNoStore(app) if onboarding is not None else app
