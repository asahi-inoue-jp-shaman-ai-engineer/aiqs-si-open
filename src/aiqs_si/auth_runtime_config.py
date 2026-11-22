"""Pure public settings renderer. Parsing or rendering never authorizes activation."""
from dataclasses import dataclass
from types import MappingProxyType

from .deployment_config import DeploymentConfig


@dataclass(frozen=True, slots=True)
class AuthRuntimeConfig:
    config: DeploymentConfig
    existing_binding: tuple
    assurance_algorithm: str

    def __post_init__(self):
        if (type(self.config) is not DeploymentConfig
                or type(self.assurance_algorithm) is not str
                or self.assurance_algorithm not in ('RS256', 'ES256')):
            raise ValueError('Invalid authentication runtime configuration')
        self.config.assert_existing_binding(self.existing_binding)

    @property
    def adapter(self):
        return MappingProxyType({
            'runtimeMode': 'configured',
            'authorityIssuer': self.config.authority_issuer,
            'upstreamIssuer': self.config.upstream_issuer,
            'federationAlias': self.config.federation_alias,
            'publicApiKey': self.config.publishable_key,
            'assuranceAlgorithm': self.assurance_algorithm,
            'getUserUrl': self.config.get_user_url,
            'jwksUrl': self.config.upstream_issuer + '/.well-known/jwks.json',
        })

    @property
    def mapper(self):
        return MappingProxyType({
            'runtimeMode': 'configured',
            'access.token.claim': 'true',
            'authorityIssuer': self.config.authority_issuer,
            'upstreamIssuer': self.config.upstream_issuer,
            'federationAlias': self.config.federation_alias,
        })

    @property
    def template(self):
        return MappingProxyType({
            'siRuntimeMode': 'configured',
            'siProviderBase': self.config.sdk_base,
            'siPublishableKey': self.config.publishable_key,
            'authorityOrigin': self.config.authority_origin,
            'cspConnectOrigin': self.config.csp_connect_origin,
        })


def render_auth_runtime_config(*, config: DeploymentConfig, existing_binding,
                               assurance_algorithm):
    """Require a preexisting pin and explicit algorithm separate from manifest v1.

    Returned maps are public, immutable values for future consumers, not a realm
    update or an assurance decision. Ledger namespaces remain in DeploymentConfig.
    """
    return AuthRuntimeConfig(config, existing_binding, assurance_algorithm)
