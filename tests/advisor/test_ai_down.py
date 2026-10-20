"""The trade explanation when the AI is down: no key, a 429 (rate limit or exhausted credit), timeouts, provider
errors, an unfinished answer. Every case gives the standard explanation of the same check with one plain sentence,
the same verdict and numbers, and never the provider's error text or a key. OpenAI is mocked everywhere."""
import asyncio
import json
import logging
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from openai import APIConnectionError, APIStatusError, APITimeoutError, RateLimitError

from egoquant.advisor.trade_explain import (AI_REJECTED, AI_UNAVAILABLE, _numbers_ok, seal_trade_context, trade_facts,
                                            trade_reply)
from tests.advisor.test_trade_explain import KEY, good, make_check

SECRET = "sk-proj-THIS-MUST-NEVER-REACH-THE-READER"
REQUEST = httpx.Request("POST", "https://api.openai.com/v1/responses")


def api_error(cls, status, message):
    return cls(message, response=httpx.Response(status, request=REQUEST), body={"error": {"message": message}})


ERRORS = {
    "rate_limit_429": api_error(RateLimitError, 429, f"Rate limit reached for key {SECRET}"),
    "credit_exhausted_429": api_error(RateLimitError, 429, f"You exceeded your current quota ({SECRET}), please check "
                                                           f"your plan and billing details. insufficient_quota"),
    "invalid_key_401": api_error(APIStatusError, 401, f"Incorrect API key provided: {SECRET}"),
    "server_error_500": api_error(APIStatusError, 500, "The server had an error"),
    "timeout": APITimeoutError(request=REQUEST),
    "connection": APIConnectionError(request=REQUEST),
    "anything_else": RuntimeError(f"boom {SECRET}"),
}


def run(coro):
    return asyncio.run(coro)


def reply_to(create, check=None, question="Why is this flagged?"):
    check = check or make_check()
    with patch.dict(os.environ, KEY):
        return run(trade_reply(seal_trade_context(check), question, create=create, base_params={"model": "m"}))


class AiDownTests(unittest.TestCase):
    def assert_standard_explanation(self, reply, sentence=AI_UNAVAILABLE):
        facts = trade_facts(make_check())
        self.assertEqual(reply.source, "template")
        self.assertEqual(reply.verdict, "not_advisable")
        self.assertIn(sentence, reply.text)                       # the reader is told, in one plain sentence
        self.assertEqual(reply.notices, [sentence])
        self.assertTrue(reply.text.startswith("This trade is outside your limits"))      # the verdict still comes first
        self.assertTrue(_numbers_ok(reply.text.rsplit("\n\n_", 2)[0], facts))      # numbers only from the check
        for forbidden in (SECRET, "sk-", "OPENAI", "quota", "billing", "RateLimit", "Traceback", "429"):
            self.assertNotIn(forbidden, reply.text + " ".join(reply.notices))

    def test_no_key(self):
        reply = reply_to(create=None)
        self.assert_standard_explanation(reply)

    def test_every_provider_failure_gives_the_same_standard_explanation(self):
        for name, error in ERRORS.items():
            with self.subTest(name):
                reply = reply_to(AsyncMock(side_effect=error))
                self.assert_standard_explanation(reply)

    def test_an_unfinished_answer(self):
        for status in ("incomplete", "failed", None):
            with self.subTest(status):
                create = AsyncMock(return_value=SimpleNamespace(status=status, output_text=json.dumps(good())))
                self.assert_standard_explanation(reply_to(create))

    def test_an_answer_that_fails_the_checks_says_so_and_is_not_shown(self):
        bad = json.dumps(good(summary="You should buy this now; it will return 12% a year."))
        create = AsyncMock(return_value=SimpleNamespace(status="completed", output_text=bad))
        reply = reply_to(create)
        self.assert_standard_explanation(reply, AI_REJECTED)
        self.assertNotIn("should buy", reply.text)
        self.assertNotIn("12%", reply.text)

    def test_the_two_sentences_are_plain_and_say_the_numbers_are_unchanged(self):
        for sentence in (AI_UNAVAILABLE, AI_REJECTED):
            self.assertIn("standard explanation of the same check", sentence)
            self.assertIn("verdict and the numbers are the same", sentence)
            self.assertNotIn("_", sentence)                       # it is wrapped in markdown italics

    def test_a_good_answer_has_no_sentence(self):
        create = AsyncMock(return_value=SimpleNamespace(status="completed", output_text=json.dumps(good())))
        reply = reply_to(create)
        self.assertEqual((reply.source, reply.notices), ("llm", []))
        self.assertNotIn("not available", reply.text)

    def test_the_audit_log_names_the_error_class_never_its_text(self):
        with self.assertLogs("egoquant.audit.chat", level=logging.INFO) as logs:
            reply_to(AsyncMock(side_effect=ERRORS["credit_exhausted_429"]))
        text = "\n".join(logs.output)
        self.assertIn("RateLimitError", text)
        self.assertNotIn(SECRET, text)
        self.assertNotIn("quota", text)


class ChatRouteTests(unittest.TestCase):
    """The same through /chat and /chat/stream, as the trade-check page calls them."""

    def setUp(self):
        from fastapi.testclient import TestClient
        from main import app
        self.client = TestClient(app)

    def body(self):                       # sealed with the test secret, so call it inside patch.dict(KEY)
        return {"messages": [{"role": "user", "content": "Explain this trade check to me."}],
                "trade_context": seal_trade_context(make_check()), "page": "portfolio"}

    def test_no_key_is_a_normal_200_with_the_sentence(self):
        with patch.dict(os.environ, {**KEY, "OPENAI_API_KEY": ""}):
            body = self.body()
            data = self.client.post("/chat", json=body).json()
            stream = self.client.post("/chat/stream", json=body).text
        self.assertIn(AI_UNAVAILABLE, data["reply"])
        self.assertEqual(data["grounding"]["notices"], [AI_UNAVAILABLE])
        self.assertIn("event: done", stream)
        self.assertIn("not available right now", stream)

    def test_a_429_from_the_provider_is_a_normal_200_with_the_sentence(self):
        manager = AsyncMock()
        create = AsyncMock(side_effect=ERRORS["credit_exhausted_429"])
        manager.__aenter__.return_value = SimpleNamespace(responses=SimpleNamespace(create=create))
        with patch.dict(os.environ, KEY), patch("chat.AsyncOpenAI", return_value=manager):
            body = self.body()
            response = self.client.post("/chat", json=body)
            stream = self.client.post("/chat/stream", json=body).text
        self.assertEqual(response.status_code, 200)               # the reader is not shown an error page
        self.assertIn(AI_UNAVAILABLE, response.json()["reply"])
        self.assertNotIn(SECRET, response.text + stream)
        self.assertNotIn("insufficient_quota", response.text + stream)
        self.assertIn("event: done", stream)


if __name__ == "__main__":
    unittest.main()
