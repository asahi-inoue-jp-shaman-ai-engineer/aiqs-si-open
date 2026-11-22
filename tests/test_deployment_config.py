from dataclasses import FrozenInstanceError
import hashlib
import unittest

from aiqs_si.deployment_config import DeploymentConfig


def manifest(**changes):
    return dict(schema_version=1, authority_issuer='https://auth.si.example/realms/si',
                resource_audience='https://api.si.example/mcp',
                upstream_issuer='https://upstream.example/auth/v1',
                publishable_key='sb_publishable_synthetic', **changes)


class DeploymentConfigTests(unittest.TestCase):
    def test_derivations_preserve_exact_ledger_namespace(self):
        config = DeploymentConfig.from_dict(manifest())
        self.assertEqual(config.authority_origin, 'https://auth.si.example')
        self.assertEqual(config.sdk_base, 'https://upstream.example')
        self.assertEqual(config.csp_connect_origin, config.sdk_base)
        self.assertEqual(config.get_user_url, 'https://upstream.example/auth/v1/user')
        self.assertEqual(config.onboarding_issuer, config.upstream_issuer)
        self.assertEqual(config.broker_identity_issuer, config.upstream_issuer)
        self.assertEqual(config.federation_alias, 'si-upstream-' + hashlib.sha256(
            b'https://upstream.example/auth/v1').hexdigest())
        self.assertEqual(config.binding, (config.authority_issuer, config.upstream_issuer, config.federation_alias))

    def test_frozen_and_detached_from_input(self):
        source = manifest()
        config = DeploymentConfig.from_dict(source)
        source['upstream_issuer'] = 'https://changed.example/auth/v1'
        self.assertEqual(config.sdk_base, 'https://upstream.example')
        with self.assertRaises(FrozenInstanceError):
            config.upstream_issuer = source['upstream_issuer']
        with self.assertRaises(FrozenInstanceError):
            del config.publishable_key

    def test_exact_manifest_fields_and_version(self):
        for value in [None, [], {}, {**manifest(), 'federation_alias': 'chosen'},
                      {**manifest(), 'secret': 'must-not-echo'},
                      {key: value for key, value in manifest().items() if key != 'publishable_key'}]:
            with self.subTest(shape=type(value).__name__), self.assertRaisesRegex(ValueError, '^Invalid deployment manifest$'):
                DeploymentConfig.from_dict(value)
        for version in [True, False, 0, 2, 1.0, '1', None]:
            with self.subTest(version=version), self.assertRaises(ValueError):
                DeploymentConfig.from_dict({**manifest(), 'schema_version': version})

    def test_every_url_rejects_noncanonical_authority(self):
        for field, path in [('authority_issuer', '/realms/si'), ('resource_audience', '/mcp'), ('upstream_issuer', '/auth/v1')]:
            for prefix in ['http://example.test', 'HTTPS://example.test', 'https://EXAMPLE.test',
                           'https://user:must-not-echo@example.test', 'https://example.test:443',
                           'https://example.test:0444', 'https://example.test:0', 'https://example.test:65536',
                           'https://example.test:', 'https://example.test.', 'https://a..test',
                           'https://-a.test', 'https://a_.test', 'https://%65xample.test',
                           'https://127.000.0.1', 'https://2130706433', 'https://0x7f000001',
                           'https://0x7f.0.0.1', 'https://example.123', 'https://[::01]',
                           'https://[fe80::1%25eth0]', 'https://例.test', ' https://example.test',
                           'https://example.test\n', 'https://example.test\\other']:
                with self.subTest(field=field, prefix=prefix), self.assertRaisesRegex(ValueError, '^Invalid deployment manifest$'):
                    DeploymentConfig.from_dict({**manifest(), field: prefix + path})

    def test_exact_paths_no_query_fragment_escape_or_traversal(self):
        for field in ['authority_issuer', 'resource_audience', 'upstream_issuer']:
            original = manifest()[field]
            for value in [original + '/', original + '?', original + '#', original + '?x=1',
                          original + '#x', original.replace('/', '/%2e/', 1),
                          'https://example.test/a/../' + original.rsplit('/', 1)[-1],
                          original.replace('https://', 'https://example.test/%2f', 1),
                          original + '%252f', original + '\x00', None, 123]:
                with self.subTest(field=field), self.assertRaises(ValueError):
                    DeploymentConfig.from_dict({**manifest(), field: value})
        for path in ['/realms/.', '/realms/..', '/realms/a/b', '/realms/', '/auth/v1']:
            with self.assertRaises(ValueError):
                DeploymentConfig.from_dict({**manifest(), 'authority_issuer': 'https://example.test' + path})
        with self.assertRaises(ValueError):
            DeploymentConfig.from_dict({**manifest(), 'authority_issuer': manifest()['upstream_issuer']})

    def test_idn_alabels_are_unsupported_even_when_valid(self):
        # Malformed A-labels were accepted here but rejected by browser URL parsing.
        # Reject valid punycode too: this initial contract intentionally has no IDN support.
        for host in ['xn--a.example', 'xn--0.example', 'xn--bcher-kva.example',
                     'auth.xn--bcher-kva.example']:
            for field, path in [('authority_issuer', '/realms/si'),
                                ('resource_audience', '/mcp'), ('upstream_issuer', '/auth/v1')]:
                with self.subTest(host=host, field=field), self.assertRaisesRegex(
                        ValueError, '^Invalid deployment manifest$'):
                    DeploymentConfig.from_dict({**manifest(), field: 'https://' + host + path})

    def test_canonical_fixture_addresses_and_nondefault_ports(self):
        for origin in ['https://localhost:9420', 'https://127.0.0.1:8443', 'https://[::1]:8443']:
            config = DeploymentConfig.from_dict({**manifest(),
                'authority_issuer': origin + '/realms/si_1-test',
                'upstream_issuer': origin + '/auth/v1'})
            self.assertEqual(config.authority_origin, origin)
            self.assertEqual(config.sdk_base, origin)
            self.assertEqual(config.get_user_url, origin + '/auth/v1/user')

    def test_only_finite_publishable_key_format(self):
        for key in ['', None, 1, 'service_role', 'sb_secret_private', 'eyJhbGciOiJIUzI1NiJ9.synthetic.signature',
                    'sb_publishable_', 'sb_publishable_a\n', 'sb_publishable_a b', 'sb_publishable_' + 'a' * 513]:
            with self.assertRaisesRegex(ValueError, '^Invalid deployment manifest$'):
                DeploymentConfig.from_dict({**manifest(), 'publishable_key': key})
        DeploymentConfig.from_dict({**manifest(), 'publishable_key': 'sb_publishable_' + 'a' * 512})

    def test_namespace_changes_reject_existing_binding_but_key_rotation_does_not(self):
        original = DeploymentConfig.from_dict(manifest())
        pin = original.binding
        original.assert_existing_binding(pin)
        rotated = DeploymentConfig.from_dict({**manifest(), 'publishable_key': 'sb_publishable_rotated'})
        rotated.assert_existing_binding(pin)
        self.assertEqual(rotated.federation_alias, original.federation_alias)
        for changes in [{'upstream_issuer': 'https://other.example/auth/v1'},
                        {'authority_issuer': 'https://auth.si.example/realms/other'}]:
            changed = DeploymentConfig.from_dict({**manifest(), **changes})
            with self.assertRaises(ValueError):
                changed.assert_existing_binding(pin)
        for malformed in [None, list(pin), pin[:-1], (*pin[:2], 'si-upstream-chosen')]:
            with self.assertRaises(ValueError):
                original.assert_existing_binding(malformed)
        self.assertEqual(pin, original.binding)

    def test_direct_constructor_also_validates(self):
        with self.assertRaises(ValueError):
            DeploymentConfig(**{**manifest(), 'resource_audience': 'http://example.test/mcp'})


if __name__ == '__main__':
    unittest.main()
