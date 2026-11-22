import unittest

import httpx

from aiqs_si.worker_token import ClientCredentialsTokenProvider, token_url_for_origin


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def make_provider(handler, clock):
    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    return ClientCredentialsTokenProvider(
        "https://asi.example/realms/si/protocol/openid-connect/token",
        "test-secret", client=client, clock=clock, wall_clock=clock)


class WorkerTokenTests(unittest.TestCase):
    def test_token_url_derivation(self):
        self.assertEqual(
            token_url_for_origin("https://asi.example/"),
            "https://asi.example/realms/si/protocol/openid-connect/token")

    def test_fetch_then_cache_until_margin(self):
        clock = Clock()
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(200, json={"access_token": "tok-%d" % len(calls),
                                             "expires_in": 3600})

        provider = make_provider(handler, clock)
        self.assertEqual(provider(), "tok-1")
        self.assertEqual(provider(), "tok-1")
        self.assertEqual(len(calls), 1)
        form = dict(httpx.QueryParams(calls[0].content.decode()))
        self.assertEqual(form["grant_type"], "client_credentials")
        self.assertEqual(form["client_id"], "si-device-worker")
        self.assertEqual(form["scope"], "aiqs:worker")
        # Inside the refresh margin the provider refetches.
        clock.now += 3600 - 30
        self.assertEqual(provider(), "tok-2")
        self.assertEqual(len(calls), 2)

    def test_rejection_is_a_fixed_message_without_secret(self):
        clock = Clock()

        def handler(request):
            return httpx.Response(401, json={"error": "invalid_client"})

        provider = make_provider(handler, clock)
        with self.assertRaises(ConnectionError) as caught:
            provider()
        self.assertNotIn("test-secret", str(caught.exception))
        self.assertNotIn("invalid_client", str(caught.exception))

    def test_invalidate_forces_refetch(self):
        clock = Clock()
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(200, json={"access_token": "tok-%d" % len(calls),
                                             "expires_in": 3600})

        provider = make_provider(handler, clock)
        self.assertEqual(provider(), "tok-1")
        provider.invalidate()
        self.assertEqual(provider(), "tok-2")

    def test_malformed_response_rejected(self):
        clock = Clock()

        def handler(request):
            return httpx.Response(200, json={"expires_in": 3600})

        provider = make_provider(handler, clock)
        with self.assertRaises(ConnectionError):
            provider()


if __name__ == "__main__":
    unittest.main()


class TokenProviderEventTests(unittest.TestCase):
    def test_events_for_fetch_refresh_and_failure(self):
        events = []
        state = {'now': 0.0, 'fail': False}

        def handler(request):
            if state['fail']:
                return httpx.Response(503, json={})
            return httpx.Response(200, json={"access_token": "tok",
                                             "expires_in": 120})

        provider = ClientCredentialsTokenProvider(
            "https://id.example/token", "s3cret",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            clock=lambda: state['now'], wall_clock=lambda: 0.0,
            on_event=events.append)
        provider()
        state['now'] = 200.0
        provider()
        state['fail'] = True
        state['now'] = 400.0
        with self.assertRaises(ConnectionError):
            provider()
        self.assertEqual(events, ['token_fetched', 'token_refreshed',
                                  'token_fetch_failed'])
