"""Native-app AI reading (native-reading-design-20261122): prompt
construction, output validation, the OpenRouter client (fake
transport only — no real provider calls), and the service's
primary/fallback discipline. Synthetic fixtures only."""
import json
import tempfile
import unittest
from pathlib import Path

import httpx

from aiqs_si.reading import (DEFAULT_FALLBACK_MODEL, DEFAULT_PRIMARY_MODEL,
                             OpenRouterClient, ReadingGenerationError,
                             ReadingProviderError, ReadingService,
                             build_reading_messages, validate_reading_text)
from aiqs_si.store import Store

BODY = "潜在意識のテーマが静かに浮かび上がり、日常の選択に新しい光を当てています。"

SUMMARY_LINES = [
    "育てることと整えることが、今の中心にあるテーマです。",
    "土台を質よく整える姿勢が、キーワードに繰り返し表れています。",
    "感情の調整が、心身のバランスの鍵になっています。",
    "効率と計画性を大切にする意識が、意図と響き合っています。",
    "自己愛のアファーメーションが、その土台を静かに支えています。",
]


def text_with_summary(summary_lines):
    return ("【要約】\n" + "\n".join(summary_lines)
            + "\n\n【キーワード群】\n" + BODY * 4 + "\n\n" + BODY * 2
            + "\n\n【インテンション】\n" + BODY * 4
            + "\n\n【自己愛アファーメーション】\n" + BODY * 4)


def valid_text():
    return text_with_summary(SUMMARY_LINES)


ENTRIES = [{"entry_id": "e1", "field_name": "分野A", "text": "本文その1",
            "hits": 128, "rank": 1},
           {"entry_id": "e2", "field_name": "分野B", "text": "本文その2",
            "hits": 121, "rank": 2}]


class PromptTests(unittest.TestCase):
    def test_prompt_carries_the_three_vectors(self):
        messages = build_reading_messages(ENTRIES, "意図の文", "自分を大切にする")
        self.assertEqual(messages[0]["role"], "system")
        user = messages[1]["content"]
        self.assertIn("分野A｜本文その1（ヒット数 128）", user)
        self.assertIn("意図の文", user)
        self.assertIn("自分を大切にする", user)
        # The system prompt fixes the session-standard reading style.
        system = messages[0]["content"]
        for heading in ("【キーワード群】", "【インテンション】",
                        "【自己愛アファーメーション】"):
            self.assertIn(heading, system)

    def test_prompt_requires_summary_first_and_paragraphs(self):
        system = build_reading_messages(ENTRIES, "意図", "肯定")[0]["content"]
        # Summary-first format (2026-11-22): a ~5-line 【要約】 block
        # opens the text, ahead of the three body headings.
        self.assertIn("【要約】", system)
        self.assertIn("5行程度", system)
        self.assertLess(system.index("【要約】"),
                        system.index("【キーワード群】"))
        # Paragraph discipline: blank line between paragraphs, no
        # mid-sentence line breaks, no bullet lists in the body.
        self.assertIn("段落の区切りでは必ず空行", system)
        self.assertIn("文の途中で改行しない", system)
        self.assertIn("箇条書きにはしない", system)

    def test_prompt_truncates_and_fences_user_fields(self):
        long = "あ" * 900
        user = build_reading_messages(ENTRIES, long, long)[1]["content"]
        self.assertNotIn("あ" * 501, user)
        self.assertIn("<<<", user)
        self.assertIn(">>>", user)

    def test_prompt_never_carries_private_context(self):
        user = build_reading_messages(ENTRIES, "意図", "肯定")[1]["content"]
        for forbidden in ("photo", "birthday", "写真", "生年月日", "entry_id", "e1"):
            self.assertNotIn(forbidden, user)


class ValidationTests(unittest.TestCase):
    def test_valid_text_passes(self):
        self.assertTrue(validate_reading_text(valid_text()))

    def test_missing_heading_fails(self):
        text = valid_text().replace("【インテンション】", "意図について")
        self.assertFalse(validate_reading_text(text))

    def test_missing_summary_fails(self):
        # The pre-2026-11-22 format (body headings only) no longer
        # passes: the summary block must open the text.
        text = ("【キーワード群】\n" + BODY * 4
                + "\n【インテンション】\n" + BODY * 4
                + "\n【自己愛アファーメーション】\n" + BODY * 4)
        self.assertFalse(validate_reading_text(text))

    def test_summary_line_count_band(self):
        # Roughly five lines: 3 and 7 pass, 2 and 8 fail.
        self.assertTrue(validate_reading_text(
            text_with_summary(SUMMARY_LINES[:3])))
        self.assertTrue(validate_reading_text(
            text_with_summary(SUMMARY_LINES + SUMMARY_LINES[:2])))
        self.assertFalse(validate_reading_text(
            text_with_summary(SUMMARY_LINES[:2])))
        self.assertFalse(validate_reading_text(
            text_with_summary(SUMMARY_LINES + SUMMARY_LINES[:3])))

    def test_body_headings_out_of_order_fail(self):
        text = ("【要約】\n" + "\n".join(SUMMARY_LINES)
                + "\n\n【キーワード群】\n" + BODY * 4
                + "\n\n【自己愛アファーメーション】\n" + BODY * 4
                + "\n\n【インテンション】\n" + BODY * 4)
        self.assertFalse(validate_reading_text(text))

    def test_length_band(self):
        self.assertFalse(validate_reading_text("【キーワード群】【インテンション】"
                                               "【自己愛アファーメーション】短い"))
        self.assertFalse(validate_reading_text(valid_text() + BODY * 60))

    def test_banned_patterns_fail(self):
        for banned in ("https://example.test", "www.example.test",
                       "OpenRouter", "Gemini", "Claude", "GPT", "言語モデル"):
            self.assertFalse(validate_reading_text(valid_text() + banned), banned)

    def test_non_string_fails(self):
        self.assertFalse(validate_reading_text(None))
        self.assertFalse(validate_reading_text(123))


class ClientTests(unittest.TestCase):
    def test_complete_posts_the_pinned_shape_and_reads_usage(self):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            seen["auth"] = request.headers.get("authorization")
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={
                "model": "google/gemini-2.5-flash",
                "choices": [{"message": {"content": valid_text()}}],
                "usage": {"prompt_tokens": 900, "completion_tokens": 1100,
                          "cost": 0.0033}})

        client = OpenRouterClient("test-key", transport=httpx.MockTransport(handler))
        result = client.complete("google/gemini-2.5-flash",
                                 [{"role": "user", "content": "x"}])
        self.assertEqual(seen["url"], "https://openrouter.ai/api/v1/chat/completions")
        self.assertEqual(seen["auth"], "Bearer test-key")
        self.assertEqual(seen["body"]["provider"], {"data_collection": "deny"})
        self.assertEqual(seen["body"]["usage"], {"include": True})
        self.assertEqual(seen["body"]["max_tokens"], 2000)
        self.assertEqual(result["text"], valid_text())
        self.assertEqual(result["prompt_tokens"], 900)
        self.assertEqual(result["completion_tokens"], 1100)
        self.assertEqual(result["provider_cost_microusd"], 3300)

    def test_http_error_and_bad_shape_raise_provider_error(self):
        def fail(request):
            return httpx.Response(503, json={"error": "down"})

        client = OpenRouterClient("k", transport=httpx.MockTransport(fail))
        with self.assertRaises(ReadingProviderError):
            client.complete("m", [])

        def bad(request):
            return httpx.Response(200, json={"choices": []})

        client = OpenRouterClient("k", transport=httpx.MockTransport(bad))
        with self.assertRaises(ReadingProviderError):
            client.complete("m", [])

    def test_key_never_echoed_in_errors(self):
        def fail(request):
            return httpx.Response(500, text="boom")

        client = OpenRouterClient("s3cr3t-key-value",
                                  transport=httpx.MockTransport(fail))
        with self.assertRaises(ReadingProviderError) as caught:
            client.complete("m", [])
        self.assertNotIn("s3cr3t-key-value", str(caught.exception))


class FakeProvider:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def complete(self, model, messages, timeout=None):
        self.calls.append(model)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return {"text": outcome, "model": model, "prompt_tokens": 10,
                "completion_tokens": 20, "provider_cost_microusd": 100}


class ServiceTests(unittest.TestCase):
    def make_service(self, provider):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        store = Store(Path(folder.name) / "svc.sqlite")
        digest = store.save_catalog({"field": [{"id": "one", "text": "test"}]})
        self.scan = {"id": "scan-1", "state": "completed", "cost": 0,
                     "payload": {"operation": "scan", "intention": "意図",
                                 "self_love_joy": "肯定",
                                 "catalog_digest": digest},
                     "result": {"scan": {"final_entries": [
                         {"id": "one", "hits": 5, "rank": 1}]}}}
        return ReadingService(store, provider)

    def test_primary_success_calls_once(self):
        provider = FakeProvider([valid_text()])
        result = self.make_service(provider).generate(self.scan)
        self.assertEqual(provider.calls, [DEFAULT_PRIMARY_MODEL])
        self.assertEqual(result["text"], valid_text())

    def test_primary_invalid_falls_back_once(self):
        provider = FakeProvider(["短い", valid_text()])
        result = self.make_service(provider).generate(self.scan)
        self.assertEqual(provider.calls,
                         [DEFAULT_PRIMARY_MODEL, DEFAULT_FALLBACK_MODEL])
        self.assertEqual(result["model"], DEFAULT_FALLBACK_MODEL)

    def test_primary_error_falls_back(self):
        provider = FakeProvider([ReadingProviderError("down"), valid_text()])
        result = self.make_service(provider).generate(self.scan)
        self.assertEqual(result["text"], valid_text())

    def test_both_invalid_is_invalid_output(self):
        provider = FakeProvider(["短い", "短い"])
        with self.assertRaises(ReadingGenerationError) as caught:
            self.make_service(provider).generate(self.scan)
        self.assertEqual(caught.exception.code, "reading_invalid_output")

    def test_both_down_is_unavailable(self):
        provider = FakeProvider([ReadingProviderError("a"),
                                 ReadingProviderError("b")])
        with self.assertRaises(ReadingGenerationError) as caught:
            self.make_service(provider).generate(self.scan)
        self.assertEqual(caught.exception.code, "reading_unavailable")

    def test_no_anthropic_model_anywhere(self):
        self.assertNotIn("anthropic", DEFAULT_PRIMARY_MODEL)
        self.assertNotIn("anthropic", DEFAULT_FALLBACK_MODEL)
        self.assertNotIn("claude", DEFAULT_PRIMARY_MODEL.lower())
        self.assertNotIn("claude", DEFAULT_FALLBACK_MODEL.lower())


if __name__ == "__main__":
    unittest.main()
