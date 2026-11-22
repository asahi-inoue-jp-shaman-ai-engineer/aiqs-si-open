"""Public routes must not mask authentication or expose non-public files."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.testclient import TestClient
from aiqs_si.application import create_application
from aiqs_si.auth import JWTVerifier
from aiqs_si.store import Store


class WebUITests(unittest.TestCase):
    def test_ui_callback_assets_and_api_boundaries(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            public = root / 'public'
            (public / 'assets').mkdir(parents=True)
            (public / 'index.html').write_text('<h1>Synthetic UI</h1>')
            (public / 'assets' / 'app.js').write_text('export const ready=true;')
            (public / 'assets' / 'debug.map').write_text('not-public')
            (public / '.env').write_text('synthetic-not-public')
            (public / 'ai-agent-guide.txt').write_text('Synthetic guide')
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048).public_key()
            issuer = 'https://issuer.example.test/auth/v1'
            user = JWTVerifier(public_key=key, issuer=issuer, audience='https://si.example.test/mcp')
            worker = JWTVerifier(public_key=key, issuer=issuer, audience='https://si.example.test/worker', required_scope='aiqs:worker')
            app = create_application(Store(root / 'store.sqlite'), user, worker,
                                     allowed_workers=['synthetic-worker'], ui_directory=public)
            with TestClient(app, base_url='http://127.0.0.1:8000') as client:
                for path in ['/', '/oauth/consent?authorization_id=synthetic']:
                    response = client.get(path)
                    self.assertEqual(response.status_code, 200)
                    self.assertIn('Synthetic UI', response.text)
                    self.assertIn("frame-ancestors 'none'", response.headers['content-security-policy'])
                    self.assertEqual(response.headers['cache-control'], 'no-store')
                    self.assertEqual(response.headers['referrer-policy'], 'no-referrer')
                self.assertEqual(client.get('/assets/app.js').status_code, 200)
                self.assertEqual(client.get('/ai-agent-guide.txt').status_code, 200)
                for path in ['/.env', '/missing', '/assets/debug.map', '/assets/%2e%2e/.env', '/assets/%5c..%5c.env']:
                    response = client.get(path)
                    self.assertEqual(response.status_code, 404, path)
                    self.assertNotIn('synthetic-not-public', response.text)
                self.assertEqual(client.post('/oauth/consent').status_code, 404)
                self.assertEqual(client.post('/mcp', json={'jsonrpc':'2.0','id':1,'method':'tools/list'}).status_code, 401)
                response = client.get('/.well-known/oauth-protected-resource/mcp')
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()['resource'], user.audience)

    def test_manifest_and_icon_routes(self):
        # The PWA manifest lives at the conventional root path while
        # the files ride the assets route; content types must be the
        # install-grade ones and the CSP must allow the manifest.
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            public = root / 'public'
            (public / 'assets').mkdir(parents=True)
            (public / 'index.html').write_text('<h1>Synthetic UI</h1>')
            (public / 'assets' / 'manifest.webmanifest').write_text('{"name":"synthetic"}')
            (public / 'assets' / 'favicon.ico').write_bytes(b'\x00\x00\x01\x00')
            (public / 'assets' / 'icon-192.png').write_bytes(b'\x89PNG')
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048).public_key()
            issuer = 'https://issuer.example.test/auth/v1'
            user = JWTVerifier(public_key=key, issuer=issuer, audience='https://si.example.test/mcp')
            worker = JWTVerifier(public_key=key, issuer=issuer, audience='https://si.example.test/worker', required_scope='aiqs:worker')
            app = create_application(Store(root / 'store.sqlite'), user, worker,
                                     allowed_workers=['synthetic-worker'], ui_directory=public)
            with TestClient(app, base_url='http://127.0.0.1:8000') as client:
                response = client.get('/manifest.webmanifest')
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers['content-type'], 'application/manifest+json')
                self.assertEqual(response.json()['name'], 'synthetic')
                self.assertIn("manifest-src 'self'", response.headers['content-security-policy'])
                response = client.get('/favicon.ico')
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers['content-type'], 'image/x-icon')
                self.assertEqual(client.get('/assets/icon-192.png').status_code, 200)
                self.assertEqual(client.get('/assets/manifest.webmanifest').status_code, 200)

    def test_app_page_follows_the_rollout_switch(self):
        # /app is served only while AIQS_SI_APP_ENABLED is on
        # (si-standalone-app-design-20261122 §8); the API stays JSON
        # either way and the page never masks it.
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            public = root / 'public'
            public.mkdir(parents=True)
            (public / 'index.html').write_text('<h1>Synthetic UI</h1>')
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048).public_key()
            issuer = 'https://issuer.example.test/auth/v1'
            user = JWTVerifier(public_key=key, issuer=issuer, audience='https://si.example.test/mcp')
            worker = JWTVerifier(public_key=key, issuer=issuer, audience='https://si.example.test/worker', required_scope='aiqs:worker')
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop('AIQS_SI_APP_ENABLED', None)
                off = create_application(Store(root / 'off.sqlite'), user, worker,
                                         allowed_workers=['synthetic-worker'], ui_directory=public)
            with TestClient(off, base_url='http://127.0.0.1:8000') as client:
                self.assertEqual(client.get('/app').status_code, 404)
            with mock.patch.dict(os.environ, {'AIQS_SI_APP_ENABLED': '1'}):
                on = create_application(Store(root / 'on.sqlite'), user, worker,
                                        allowed_workers=['synthetic-worker'], ui_directory=public)
            with TestClient(on, base_url='http://127.0.0.1:8000') as client:
                response = client.get('/app')
                self.assertEqual(response.status_code, 200)
                self.assertIn('Synthetic UI', response.text)
