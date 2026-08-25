import asyncio
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

try:
    import openai  # noqa: F401
except ModuleNotFoundError:
    openai_stub = types.ModuleType("openai")
    openai_stub.OpenAI = Mock
    openai_stub.RateLimitError = type("RateLimitError", (Exception,), {})
    sys.modules["openai"] = openai_stub

try:
    import requests  # noqa: F401
except ModuleNotFoundError:
    sys.modules["requests"] = types.ModuleType("requests")

from discord_tron_master.classes.openai.text import GPT


class FakeConfig:
    def __init__(self, api_key="test-key"):
        self.api_key = api_key

    def get_openai_api_key(self):
        return self.api_key

    def normalize_zork_backend(self, backend):
        return backend


def make_gpt(api_key="test-key"):
    gpt = object.__new__(GPT)
    gpt.config = FakeConfig(api_key)
    gpt.engine = "o3-mini"
    gpt.temperature = 0.9
    gpt.max_tokens = 4096
    gpt.backend = "zai"
    gpt.discord_bot_role = "You are a Discord bot."
    return gpt


class GPTRequestTests(unittest.TestCase):
    def test_zai_uses_api_key_directly_with_coding_endpoint(self):
        gpt = make_gpt("  test-key  ")
        response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="hello"))]
        )
        create = Mock(return_value=response)
        client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        )

        with (
            patch(
                "discord_tron_master.classes.openai.text.OpenAI",
                return_value=client,
            ) as openai_client,
            patch("uuid.uuid4", return_value="session-id"),
        ):
            result = gpt._send_zai_openai_request(
                [{"role": "user", "content": "hello"}]
            )

        self.assertEqual(result, "hello")
        openai_client.assert_called_once_with(
            api_key="test-key",
            base_url="https://api.z.ai/api/coding/paas/v4",
            default_headers={
                "User-Agent": "opencode/1.4.3",
                "x-session-affinity": "session-id",
            },
        )
        create.assert_called_once_with(
            model="glm-5-turbo",
            messages=[{"role": "user", "content": "hello"}],
            temperature=0.9,
            max_tokens=4096,
            stream=False,
        )

    def test_zai_rejects_missing_api_key(self):
        gpt = make_gpt(None)

        with self.assertRaisesRegex(ValueError, "API key is not configured"):
            gpt._send_zai_openai_request(
                [{"role": "user", "content": "hello"}]
            )

    def test_zai_provider_error_is_not_converted_to_none(self):
        gpt = make_gpt()
        gpt._send_zai_openai_request = Mock(
            side_effect=RuntimeError("401 token expired or incorrect")
        )

        with self.assertRaisesRegex(RuntimeError, "401 token expired or incorrect"):
            asyncio.run(gpt.turbo_completion("system prompt", "hello"))

        gpt._send_zai_openai_request.assert_called_once_with(
            [
                {"role": "system", "content": "system prompt"},
                {"role": "user", "content": "hello"},
            ]
        )

    def test_zai_rejects_empty_provider_content(self):
        gpt = make_gpt()
        response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=None))]
        )
        create = Mock(return_value=response)
        client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        )

        with patch(
            "discord_tron_master.classes.openai.text.OpenAI",
            return_value=client,
        ):
            with self.assertRaisesRegex(ValueError, "empty text response"):
                gpt._send_zai_openai_request(
                    [{"role": "user", "content": "hello"}]
                )


if __name__ == "__main__":
    unittest.main()
