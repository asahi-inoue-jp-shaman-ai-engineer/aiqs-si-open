"""Opt-in onboarding ASGI factory; no listener, configuration loading or activation."""
import re

from starlette.applications import Starlette
from starlette.datastructures import MutableHeaders

from .deployment_config import DeploymentConfig
from .onboarding import Onboarding
from .onboarding_provider import SupabaseOnboardingProvider
from .store import Store


def create_onboarding_app(*, config: DeploymentConfig, existing_binding,
                          store: Store, hook_secret, upstream_transport=None):
    """Use an existing namespace pin and migration seal; never establish either.

    An injected transport is for controlled server-side HTTP testing. Each
    finalization still creates a fresh client with no shared browser cookie jar.
    """
    if type(config) is not DeploymentConfig or not isinstance(store, Store):
        raise ValueError('Invalid onboarding configuration')
    config.assert_existing_binding(existing_binding)
    with store.transaction() as db:
        seal = db.execute('SELECT manifest_digest FROM identity_migrations WHERE issuer=?',
                          (config.onboarding_issuer,)).fetchone()
        if seal is None or not isinstance(seal['manifest_digest'], str) or re.fullmatch(
                r'[0-9a-f]{64}', seal['manifest_digest']) is None:
            raise ValueError('Migration reconciliation required')
    coordinator = Onboarding(store, issuer=config.onboarding_issuer)
    provider = SupabaseOnboardingProvider(coordinator, issuer=config.upstream_issuer,
        publishable_key=config.publishable_key, hook_secret=hook_secret, transport=upstream_transport)
    app = Starlette(routes=provider.routes())
    app.router.redirect_slashes = False

    # Wrap the entire Starlette stack, including its generated 404/405/500 errors.
    async def no_store_app(scope, receive, send):
        async def no_store_send(message):
            if message['type'] == 'http.response.start':
                headers = MutableHeaders(scope=message)
                headers['Cache-Control'] = 'no-store'
                headers['Pragma'] = 'no-cache'
            await send(message)
        await app(scope, receive, no_store_send)

    return no_store_app
