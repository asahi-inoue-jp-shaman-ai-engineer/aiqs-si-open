import json
import unittest

from aiqs_si.auth_runtime_config import render_auth_runtime_config
from aiqs_si.deployment_config import DeploymentConfig
from aiqs_si.realm_candidate import build_realm_candidate


class RealmCandidateTests(unittest.TestCase):
    def runtime(self):
        config = DeploymentConfig(1, 'https://si.example.test/realms/si',
            'https://si.example.test/mcp', 'https://upstream.example.test/auth/v1',
            'sb_publishable_synthetic')
        return render_auth_runtime_config(config=config,
            existing_binding=config.binding, assurance_algorithm='ES256')

    def test_candidate_cannot_enable_clients_or_import_users(self):
        candidate = build_realm_candidate(self.runtime())
        self.assertFalse(candidate['enabled'])
        self.assertEqual(candidate['sslRequired'], 'all')
        self.assertEqual(candidate['users'], [])
        self.assertEqual(len(candidate['clients']), 1)
        resource = candidate['clients'][0]
        self.assertTrue(resource['bearerOnly'])
        self.assertFalse(resource['enabled'])
        self.assertFalse(resource['standardFlowEnabled'])
        self.assertFalse(resource['directAccessGrantsEnabled'])
        self.assertFalse(resource['serviceAccountsEnabled'])
        provider = candidate['identityProviders'][0]
        self.assertFalse(provider['enabled'])
        self.assertFalse(provider['storeToken'])
        self.assertFalse(provider['trustEmail'])
        self.assertNotIn('credentials', json.dumps(candidate))

    def test_exact_identity_and_adapter_settings_and_detachment(self):
        runtime = self.runtime()
        a = build_realm_candidate(runtime)
        self.assertEqual(a['realm'], 'si')
        config = a['authenticatorConfig'][0]['config']
        self.assertEqual(config['entryMode'], 'public-unified')
        self.assertEqual(config['runtimeMode'], 'configured')
        self.assertEqual(config['federationAlias'], runtime.config.federation_alias)
        self.assertEqual(config['upstreamIssuer'], runtime.config.upstream_issuer)
        json.dumps(a)
        config['upstreamIssuer'] = 'changed'
        self.assertEqual(build_realm_candidate(runtime)['authenticatorConfig'][0]
            ['config']['upstreamIssuer'], runtime.config.upstream_issuer)

    def test_raw_or_missing_binding_cannot_be_rendered(self):
        for value in (None, {}, self.runtime().config):
            with self.assertRaises(ValueError):
                build_realm_candidate(value)
        runtime = self.runtime()
        with self.assertRaises(ValueError):
            render_auth_runtime_config(config=runtime.config,
                existing_binding=('wrong', 'wrong', 'wrong'),
                assurance_algorithm='ES256')

    def test_registration_constraints_cover_anonymous_and_authenticated(self):
        realm = build_realm_candidate(self.runtime())
        policies = realm['components'][
            'org.keycloak.services.clientregistration.policy.ClientRegistrationPolicy']
        for subtype in ('anonymous', 'authenticated'):
            selected = {p['providerId']: p['config'] for p in policies
                        if p['subType'] == subtype}
            self.assertEqual(len(selected), 6)
            self.assertEqual(selected['allowed-protocol-mappers'],
                             {'allowed-protocol-mapper-types': []})
            self.assertEqual(selected['allowed-client-templates']['allowed-client-scopes'],
                             ['openid', 'aiqs:use', 'si-contract'])
            self.assertEqual(selected['allowed-client-templates']['allow-default-scopes'], ['false'])
            self.assertEqual(selected['max-clients'], {'max-clients': ['100']})
            for required in ('consent-required', 'scope', 'si-lab-access-token-profile'):
                self.assertIn(required, selected)
        self.assertFalse(realm['enabled'])
        self.assertEqual(realm['users'], [])

    def test_realm_owned_contract_preserves_identity_and_resource(self):
        runtime = self.runtime()
        realm = build_realm_candidate(runtime)
        self.assertEqual(realm['defaultDefaultClientScopes'], ['si-contract'])
        self.assertEqual(realm['defaultOptionalClientScopes'], ['aiqs:use'])
        scopes = {s['name']: s for s in realm['clientScopes']}
        mappers = {m['name']: m for m in scopes['si-contract']['protocolMappers']}
        self.assertEqual(mappers['verified-upstream']['config'], dict(runtime.mapper))
        self.assertEqual(mappers['subject']['protocolMapper'], 'oidc-sub-mapper')
        self.assertEqual(mappers['client-id']['protocolMapper'], 'si-lab-client-id')
        self.assertEqual(mappers['si-audience']['config']['included.client.audience'],
                         realm['clients'][0]['clientId'])
        self.assertEqual(realm['clients'][0]['attributes']['resource_url'],
                         runtime.config.resource_audience)
        self.assertEqual(scopes['aiqs:use']['attributes']['include.in.token.scope'], 'true')

    def test_pkce_policy_is_linked_without_external_client_registration(self):
        realm = build_realm_candidate(self.runtime())
        profile = realm['clientProfiles']['profiles'][0]
        policy = realm['clientPolicies']['policies'][0]
        self.assertEqual(policy['profiles'], [profile['name']])
        self.assertEqual(profile['executors'][0]['executor'], 'pkce-enforcer')
        self.assertTrue(policy['enabled'])
        self.assertFalse(realm['enabled'])
        self.assertTrue(all(c['bearerOnly'] for c in realm['clients']))

    def test_token_lifetimes_and_locale_are_explicit(self):
        candidate = build_realm_candidate(self.runtime())
        self.assertEqual(candidate['accessTokenLifespan'], 3600)
        self.assertEqual(candidate['ssoSessionIdleTimeout'], 2592000)
        self.assertEqual(candidate['ssoSessionMaxLifespan'], 15552000)
        self.assertTrue(candidate['internationalizationEnabled'])
        self.assertEqual(candidate['defaultLocale'], 'ja')
        self.assertEqual(candidate['supportedLocales'], ['ja', 'en'])
