import asyncio
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

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
    def __init__(
        self,
        api_key="test-key",
        model="glm-5-turbo",
        enable_mcp_tools=True,
    ):
        self.api_key = api_key
        self.model = model
        self.enable_mcp_tools = enable_mcp_tools

    def get_openai_api_key(self):
        return self.api_key

    def get_openai_model(self):
        return self.model

    def get_openai_mcp_tools_enabled(self):
        return self.enable_mcp_tools

    def get_user_setting(self, _user_id, _setting, default=None):
        return default

    def normalize_zork_backend(self, backend):
        return backend


def make_gpt(api_key="test-key", model="glm-5-turbo"):
    gpt = object.__new__(GPT)
    gpt.config = FakeConfig(api_key, model)
    gpt.engine = "o3-mini"
    gpt.temperature = 0.9
    gpt.max_tokens = 4096
    gpt.backend = "zai"
    gpt.discord_bot_role = "You are a Discord bot."
    return gpt


class GPTRequestTests(unittest.TestCase):
    def test_zai_uses_api_key_directly_with_coding_endpoint(self):
        gpt = make_gpt("  test-key  ", model="glm-5.1")
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
                "User-Agent": "opencode/1.15.13",
                "x-session-affinity": "session-id",
            },
        )
        create.assert_called_once_with(
            model="glm-5.1",
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
            ],
            enable_tools=False,
        )

    def test_discord_chat_enables_tools_and_mentions(self):
        gpt = make_gpt()
        gpt.turbo_completion = AsyncMock(return_value="<@123>")
        ctx = SimpleNamespace(author=SimpleNamespace(id=42))

        result = asyncio.run(
            gpt.discord_bot_response('ping <@123>', ctx=ctx)
        )

        self.assertEqual(result, "<@123>")
        role, prompt = gpt.turbo_completion.await_args.args
        options = gpt.turbo_completion.await_args.kwargs
        self.assertIn("responding directly inside Discord", role)
        self.assertIn("Never claim that you cannot send a Discord mention", role)
        self.assertEqual(prompt, 'ping <@123>')
        self.assertTrue(options["enable_tools"])

    def test_explicit_discord_mention_is_preserved_in_response(self):
        response = GPT.ensure_requested_discord_mentions(
            "can you ping <@123>",
            "Sure, one moment.",
        )

        self.assertEqual(response, "Sure, one moment.\n<@123>")

    def test_unrequested_discord_mention_is_not_added(self):
        response = GPT.ensure_requested_discord_mentions(
            "what did <@123> say?",
            "I don't know.",
        )

        self.assertEqual(response, "I don't know.")

    def test_zai_chat_exposes_search_reader_and_github_tools(self):
        gpt = make_gpt()
        response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="result"))]
        )
        create = Mock(return_value=response)
        client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        )

        with patch(
            "discord_tron_master.classes.openai.text.OpenAI",
            return_value=client,
        ):
            gpt._send_zai_openai_request(
                [{"role": "user", "content": "search the web"}],
                enable_tools=True,
            )

        options = create.call_args.kwargs
        self.assertEqual(options["tool_choice"], "auto")
        tools = {tool["mcp"]["server_label"]: tool for tool in options["tools"]}
        self.assertEqual(
            set(tools),
            {"web-search-prime", "web-reader", "zread"},
        )
        self.assertEqual(
            tools["zread"]["mcp"]["allowed_tools"],
            ["search_doc", "get_repo_structure", "read_file"],
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
