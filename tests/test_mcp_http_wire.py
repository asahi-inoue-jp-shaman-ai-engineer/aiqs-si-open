"""Loopback TCP + standard-library HTTP client, no MCP client SDK or live keys."""
from aiqs_si.auth import create_mcp_http_app
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

from cryptography.hazmat.primitives.asymmetric import rsa
import jwt
import uvicorn
from aiqs_si.auth import JWTVerifier, create_http_server
from aiqs_si.store import Store
from aiqs_si.application import create_application


class MCPHTTPWireTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.issuer = 'https://issuer.example.test/auth/v1'
        self.audience = 'https://si.example.test/mcp'
        store = Store(Path(folder.name) / 'synthetic.sqlite')
        verifier = JWTVerifier(public_key=self.key.public_key(), issuer=self.issuer,
                               audience=self.audience)
        worker = JWTVerifier(public_key=self.key.public_key(), issuer=self.issuer,
                             audience='https://si.example.test', required_scope='aiqs:worker')
        app = create_application(store, verifier, worker, allowed_workers={'synthetic-worker'})
        sock = socket.socket()
        self.addCleanup(sock.close)
        sock.bind(('127.0.0.1', 0))
        self.base = 'http://127.0.0.1:' + str(sock.getsockname()[1])
        self.server = uvicorn.Server(uvicorn.Config(app, log_level='critical', access_log=False))
        self.thread = threading.Thread(target=self.server.run, kwargs={'sockets': [sock]}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        deadline = time.monotonic() + 10
        while not self.server.started and self.thread.is_alive() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue(self.server.started, 'Loopback test server did not start')
        self.http = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def stop_server(self):
        self.server.should_exit = True
        self.thread.join(10)
        self.assertFalse(self.thread.is_alive(), 'Loopback test server did not stop')

    def token(self, **changes):
        claims = dict(iss=self.issuer, aud=self.audience, sub='synthetic-user',
                      client_id='independent-test-client', scope='aiqs:use',
                      iat=int(time.time()), exp=int(time.time()) + 60)
        claims.update(changes)
        return jwt.encode(claims, self.key, algorithm='RS256', headers={'typ': 'at+jwt'})

    def request(self, path='/mcp', *, payload=None, token=None, version='2025-11-25', method_header=None):
        headers = {'Accept': 'application/json, text/event-stream', 'MCP-Protocol-Version': version}
        if token:
            headers['Authorization'] = 'Bearer ' + token
        data = None
        if payload is not None:
            headers['Content-Type'] = 'application/json'
            if version == '2026-07-28':
                headers['MCP-Method'] = method_header or payload['method']
                if payload['method'] == 'tools/call':
                    headers['MCP-Name'] = payload['params']['name']
            data = json.dumps(payload).encode()
        req = urllib.request.Request(self.base + path, data=data, headers=headers)
        try:
            response = self.http.open(req, timeout=5)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, response.headers, json.loads(response.read())

    def test_discovery_authentication_and_tool_call_over_real_http(self):
        request = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list', 'params': {}}
        status, headers, _ = self.request(payload=request)
        self.assertEqual(status, 401)
        self.assertIn('resource_metadata', headers['WWW-Authenticate'])
        status, _, metadata = self.request('/.well-known/oauth-protected-resource/mcp')
        self.assertEqual(status, 200)
        self.assertEqual(metadata['resource'], self.audience)
        self.assertEqual(metadata['authorization_servers'], [self.issuer])
        token = self.token()
        status, _, listing = self.request(payload=request, token=token)
        self.assertEqual(status, 200)
        self.assertIn('credit_balance', {tool['name'] for tool in listing['result']['tools']})
        request.update(method='tools/call', params={'name': 'credit_balance', 'arguments': {}})
        status, _, result = self.request(payload=request, token=token)
        self.assertEqual(status, 200)
        self.assertEqual(result['id'], 1)
        self.assertEqual(result['result']['structuredContent']['available'], 0)
        for changes in ({'aud': 'https://other.example.test'}, {'exp': int(time.time()) - 30}):
            status, _, _ = self.request(payload=request, token=self.token(**changes))
            self.assertEqual(status, 401)

    def test_unsupported_protocol_version_is_not_silently_accepted(self):
        request = {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list', 'params': {}}
        status, _, _ = self.request(payload=request, token=self.token(), version='2099-01-01')
        self.assertEqual(status, 400)

    def test_modern_discovery_and_tool_call_without_initialization(self):
        meta = {'io.modelcontextprotocol/protocolVersion': '2026-07-28',
                'io.modelcontextprotocol/clientCapabilities': {}}
        request = {'jsonrpc': '2.0', 'id': 'modern', 'method': 'server/discover',
                   'params': {'_meta': meta}}
        status, _, result = self.request(payload=request, token=self.token(), version='2026-07-28')
        self.assertEqual(status, 200, result)
        self.assertIn('2026-07-28', result['result']['supportedVersions'])
        request.update(method='tools/call', params={'_meta': meta, 'name': 'credit_balance', 'arguments': {}})
        status, _, result = self.request(payload=request, token=self.token(), version='2026-07-28')
        self.assertEqual(status, 200, result)
        self.assertEqual(result['result']['structuredContent']['available'], 0)

    def test_modern_method_header_mismatch_is_rejected(self):
        request = {'jsonrpc': '2.0', 'id': 'mismatch', 'method': 'tools/list',
                   'params': {'_meta': {'io.modelcontextprotocol/protocolVersion': '2026-07-28',
                                       'io.modelcontextprotocol/clientCapabilities': {}}}}
        status, _, result = self.request(payload=request, token=self.token(),
                                         version='2026-07-28', method_header='tools/call')
        self.assertEqual(status, 400)
        self.assertEqual(result['error']['code'], -32020)

    def test_method_error_preserves_auth_and_invalid_params(self):
        request = {'jsonrpc': '2.0', 'id': 'unknown-1', 'method': 'missing/method', 'params': {}}
        status, _, _ = self.request(payload=request)
        self.assertEqual(status, 401)
        status, _, result = self.request(payload=request, token=self.token())
        self.assertEqual(status, 200)
        self.assertEqual(result['id'], 'unknown-1')
        self.assertEqual(result['error']['code'], -32601)
        request['method'] = 'tools/call'
        status, _, result = self.request(payload=request, token=self.token())
        self.assertEqual(result['error']['code'], -32602)

    def test_unauthenticated_delayed_body_returns_401_without_connection_reset(self):
        body = b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
        from urllib.parse import urlsplit
        target = urlsplit(self.base)
        with socket.create_connection((target.hostname, target.port), timeout=5) as connection:
            headers = ('POST /mcp HTTP/1.1\r\nHost: '+target.netloc+
                       '\r\nConnection: close\r\nContent-Type: application/json\r\nContent-Length: '+
                       str(len(body))+'\r\n\r\n').encode()
            connection.sendall(headers)
            time.sleep(.03)  # Deliberate split arrival, not a readiness workaround.
            connection.sendall(body)
            import http.client
            response = http.client.HTTPResponse(connection)
            response.begin()
            self.assertEqual(response.status, 401)
            self.assertIn('resource_metadata', response.headers['WWW-Authenticate'])
            self.assertTrue(response.read())


if __name__ == '__main__':
    unittest.main()
