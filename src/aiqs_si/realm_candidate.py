"""Build a disabled SI realm candidate; never import or activate it."""
from .auth_runtime_config import AuthRuntimeConfig


def build_realm_candidate(runtime: AuthRuntimeConfig) -> dict:
    """Require a previously validated runtime binding, not a synthetic lab realm.

    External clients must be registered and reviewed before activation.
    This deliberately cannot produce an enabled login service.
    """
    if type(runtime) is not AuthRuntimeConfig:
        raise ValueError('Validated authentication runtime required')
    realm = runtime.config.authority_issuer.rsplit('/', 1)[1]
    return {
        'realm': realm,
        'enabled': False,
        'sslRequired': 'all',
        'registrationAllowed': False,
        'resetPasswordAllowed': False,
        'rememberMe': False,
        'revokeRefreshToken': True,
        'refreshTokenMaxReuse': 0,
        # Token/session lifetimes (Asahi review, 2026-11-19): Keycloak defaults
        # (5-minute access tokens, 30-minute SSO idle) cut off real MCP users
        # between sessions, so the realm carries explicit values instead.
        # Access tokens stay short at 1 hour; the refresh path follows the SSO
        # session, which now idles at 30 days with a 180-day maximum.
        'accessTokenLifespan': 3600,
        'ssoSessionIdleTimeout': 2592000,
        'ssoSessionMaxLifespan': 15552000,
        # Japanese-first UI for Japanese users (applied to production
        # 2026-11-19 at Asahi's request; kept here so rebuilds preserve it).
        'internationalizationEnabled': True,
        'supportedLocales': ['ja', 'en'],
        'defaultLocale': 'ja',
        'users': [],
        'clients': [{
            'clientId': 'si-resource', 'enabled': False,
            'protocol': 'openid-connect', 'bearerOnly': True,
            'standardFlowEnabled': False, 'directAccessGrantsEnabled': False,
            'serviceAccountsEnabled': False,
            'attributes': {'resource_url': runtime.config.resource_audience},
        }],
        'defaultDefaultClientScopes': ['si-contract'],
        'defaultOptionalClientScopes': ['aiqs:use'],
        'components': {
            'org.keycloak.services.clientregistration.policy.ClientRegistrationPolicy': [
                {'name': subtype + '-' + provider, 'providerId': provider,
                 'subType': subtype, 'config': config}
                for subtype in ('anonymous', 'authenticated')
                for provider, config in (
                    ('allowed-protocol-mappers', {'allowed-protocol-mapper-types': []}),
                    ('allowed-client-templates', {
                        'allowed-client-scopes': ['openid', 'aiqs:use', 'si-contract'],
                        'allow-default-scopes': ['false']}),
                    ('consent-required', {}),
                    ('scope', {}),
                    ('si-lab-access-token-profile', {}),
                    ('max-clients', {'max-clients': ['100']}),
                )
            ],
        },
        'clientScopes': [{
            'name': 'si-contract', 'protocol': 'openid-connect',
            'attributes': {'display.on.consent.screen': 'false',
                           'include.in.token.scope': 'false'},
            'protocolMappers': [{
                'name': 'si-audience', 'protocol': 'openid-connect',
                'protocolMapper': 'oidc-audience-mapper',
                'config': {
                    'included.client.audience': 'si-resource',
                    'access.token.claim': 'true', 'id.token.claim': 'false',
                },
            }, {
                'name': 'subject', 'protocol': 'openid-connect',
                'protocolMapper': 'oidc-sub-mapper',
                'config': {'access.token.claim': 'true'},
            }, {
                'name': 'client-id', 'protocol': 'openid-connect',
                'protocolMapper': 'si-lab-client-id',
                'config': {'access.token.claim': 'true'},
            }, {
                'name': 'verified-upstream', 'protocol': 'openid-connect',
                'protocolMapper': 'si-lab-upstream-identity',
                'config': dict(runtime.mapper),
            }],
        }, {
            'name': 'aiqs:use', 'protocol': 'openid-connect',
            'attributes': {
                'include.in.token.scope': 'true',
                'display.on.consent.screen': 'true',
                'consent.screen.text': 'アイクスSIの利用（照射時にクレジットを消費）',
            },
        }],
        'clientProfiles': {'profiles': [{
            'name': 'si-pkce', 'executors': [{
                'executor': 'pkce-enforcer', 'configuration': {'auto-configure': True},
            }],
        }]},
        'clientPolicies': {'policies': [{
            'name': 'si-pkce', 'enabled': True,
            'conditions': [{'condition': 'any-client', 'configuration': {}}],
            'profiles': ['si-pkce'],
        }]},
        'identityProviders': [{
            'alias': runtime.config.federation_alias,
            'providerId': 'oidc',
            'enabled': False,
            'storeToken': False,
            'trustEmail': False,
            'config': {'issuer': runtime.config.upstream_issuer},
        }],
        'browserFlow': 'si-independent-browser',
        'authenticationFlows': [{
            'alias': 'si-independent-browser', 'providerId': 'basic-flow',
            'topLevel': True, 'builtIn': False,
            'authenticationExecutions': [{
                'authenticator': 'si-independent-upstream',
                'requirement': 'REQUIRED', 'priority': 10,
                'authenticatorFlow': False,
                'authenticatorConfig': 'si-independent-config',
            }],
        }],
        'authenticatorConfig': [{
            'alias': 'si-independent-config',
            'config': {**runtime.adapter, 'entryMode': 'public-unified'},
        }],
    }
