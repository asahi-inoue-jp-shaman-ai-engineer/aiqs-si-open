from dataclasses import FrozenInstanceError
import unittest

from aiqs_si.auth_runtime_config import AuthRuntimeConfig, render_auth_runtime_config
from aiqs_si.deployment_config import DeploymentConfig


class AuthRuntimeConfigTests(unittest.TestCase):
    def config(self, upstream='https://upstream.example/auth/v1', key='sb_publishable_synthetic'):
        return DeploymentConfig(1, 'https://authority.example/realms/si',
                                'https://resource.example/mcp', upstream, key)

    def render(self, config=None, algorithm='ES256'):
        config = config or self.config()
        return render_auth_runtime_config(config=config, existing_binding=config.binding,
                                          assurance_algorithm=algorithm)

    def test_two_configurations_consistent_across_consumers(self):
        results = []
        for upstream in ['https://upstream.example/auth/v1', 'https://localhost:9420/auth/v1']:
            config = self.config(upstream)
            runtime = self.render(config)
            self.assertEqual(dict(runtime.mapper), {'access.token.claim': 'true', **{key: runtime.adapter[key] for key in
                ('runtimeMode', 'authorityIssuer', 'upstreamIssuer', 'federationAlias')}})
            self.assertEqual(runtime.adapter['runtimeMode'], 'configured')
            self.assertEqual(runtime.template['siRuntimeMode'], 'configured')
            self.assertEqual(runtime.adapter['federationAlias'], config.federation_alias)
            self.assertEqual(runtime.adapter['getUserUrl'], upstream + '/user')
            self.assertEqual(runtime.adapter['jwksUrl'], upstream + '/.well-known/jwks.json')
            self.assertEqual(runtime.template['siProviderBase'] + '/auth/v1', upstream)
            self.assertEqual(runtime.template['cspConnectOrigin'], runtime.template['siProviderBase'])
            self.assertEqual(runtime.template['siPublishableKey'], runtime.adapter['publicApiKey'])
            self.assertEqual(runtime.template['authorityOrigin'], 'https://authority.example')
            self.assertEqual(config.broker_identity_issuer, upstream)
            self.assertEqual(config.onboarding_issuer, upstream)
            results.append(runtime)
        self.assertNotEqual(results[0].mapper['federationAlias'], results[1].mapper['federationAlias'])

    def test_pin_mismatch_missing_and_mutable_pins_rejected(self):
        config = self.config()
        for pin in [None, list(config.binding), config.binding[:-1],
                    ('https://other.example/realms/si', *config.binding[1:]),
                    (config.authority_issuer, 'https://other.example/auth/v1', config.federation_alias),
                    (*config.binding[:2], 'chosen-alias')]:
            with self.assertRaises(ValueError):
                render_auth_runtime_config(config=config, existing_binding=pin, assurance_algorithm='ES256')

    def test_key_rotation_changes_only_public_key_settings(self):
        config = self.config()
        rotated = self.config(key='sb_publishable_rotated')
        first = self.render(config)
        second = render_auth_runtime_config(config=rotated, existing_binding=config.binding, assurance_algorithm='ES256')
        self.assertEqual(first.mapper, second.mapper)
        self.assertEqual(first.existing_binding, second.existing_binding)
        self.assertEqual({k: v for k, v in first.adapter.items() if k != 'publicApiKey'},
                         {k: v for k, v in second.adapter.items() if k != 'publicApiKey'})
        self.assertEqual(second.template['siPublishableKey'], 'sb_publishable_rotated')

    def test_required_explicit_algorithm_and_direct_constructor_validation(self):
        config = self.config()
        for algorithm in [None, '', 'HS256', 'none', 'es256', 'ES256 ', 1, True, ['ES256']]:
            with self.assertRaisesRegex(ValueError, '^Invalid authentication runtime configuration$'):
                self.render(config, algorithm)
        for algorithm in ['RS256', 'ES256']:
            self.assertEqual(self.render(config, algorithm).adapter['assuranceAlgorithm'], algorithm)
        with self.assertRaises(TypeError):
            render_auth_runtime_config(config=config, existing_binding=config.binding)
        with self.assertRaises(ValueError):
            AuthRuntimeConfig(config, None, 'ES256')
        for invalid in [None, {}, 'private-value']:
            with self.assertRaisesRegex(ValueError, '^Invalid authentication runtime configuration$'):
                render_auth_runtime_config(config=invalid, existing_binding=config.binding, assurance_algorithm='ES256')

    def test_value_and_all_consumer_maps_are_immutable(self):
        runtime = self.render()
        with self.assertRaises(FrozenInstanceError):
            runtime.assurance_algorithm = 'RS256'
        for mapping in [runtime.adapter, runtime.mapper, runtime.template]:
            with self.assertRaises(TypeError):
                mapping['injected'] = 'value'
            with self.assertRaises(TypeError):
                del mapping[next(iter(mapping))]
        copied = dict(runtime.adapter)
        copied['upstreamIssuer'] = 'https://other.example/auth/v1'
        self.assertEqual(runtime.adapter['upstreamIssuer'], runtime.config.upstream_issuer)

    def test_canonical_ipv6_derivation_and_rejection_remain_manifest_owned(self):
        runtime = self.render(self.config('https://[::1]:9420/auth/v1'))
        self.assertEqual(runtime.template['siProviderBase'], 'https://[::1]:9420')
        self.assertEqual(runtime.adapter['jwksUrl'], 'https://[::1]:9420/auth/v1/.well-known/jwks.json')
        for upstream in ['http://upstream.example/auth/v1', 'https://upstream.example/auth/v1/',
                         'https://upstream.example:443/auth/v1', 'https://upstream.example/auth/%76%31']:
            with self.assertRaises(ValueError):
                self.config(upstream)


if __name__ == '__main__':
    unittest.main()
