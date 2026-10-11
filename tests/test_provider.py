"""DeepSeek adapter wire format, checked against a mock HTTP transport (no network)."""

from __future__ import annotations

import json

import httpx2 as httpx
import pytest

from whim import credentials
from whim.config import ProviderConfig
from whim.credentials import Secret
from whim.provider.base import ProviderError, Request
from whim.provider.deepseek import DeepSeekProvider

KEY = "sk-0123456789abcdef0123456789abcdef"

RESPONSE = {
    "id": "x",
    "object": "chat.completion",
    "created": 0,
    "model": "deepseek-v4-pro",
    "choices": [{
        "index": 0,
        "finish_reason": "tool_calls",
        "message": {
            "role": "assistant",
            "content": None,
            "reasoning_content": "private chain",
            "tool_calls": [{"id": "call_00_a", "type": "function",
                            "function": {"name": "turn_finish", "arguments": "{}"}}],
        },
    }],
    "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120,
              "prompt_cache_hit_tokens": 64, "prompt_cache_miss_tokens": 36,
              "completion_tokens_details": {"reasoning_tokens": 12}},
}


def make(handler, **cfg):
    pc = ProviderConfig(cache_user_id="character_abc", **cfg)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return DeepSeekProvider(pc, api_key=Secret(KEY), http_client=client)


async def test_request_body_and_response_parsing():
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(json.loads(req.content))
        return httpx.Response(200, json=RESPONSE)

    p = make(handler)
    history = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": None, "reasoning_content": "earlier chain",
         "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "file_read", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "{}"},
    ]
    tools = [{"type": "function", "function": {"name": "turn_finish", "description": "", "parameters": {}}}]
    c = await p.complete(Request(messages=history, tools=tools))

    body = seen[0]
    assert body["user_id"] == "character_abc"
    assert body["thinking"] == {"type": "enabled"}
    assert body["reasoning_effort"] == "high"
    assert body["messages"][2]["reasoning_content"] == "earlier chain"  # replayed verbatim
    assert "tool_choice" not in body  # `required` is rejected in thinking mode
    assert c.reasoning_content == "private chain"
    assert c.tool_calls[0].name == "turn_finish"
    assert c.usage["cache_hit_tokens"] == 64 and c.usage["cache_miss_tokens"] == 36
    assert c.usage["reasoning_tokens"] == 12
    assert c.assistant_message()["reasoning_content"] == "private chain"


async def test_thinking_disabled_sends_no_effort():
    seen = []

    def handler(req):
        seen.append(json.loads(req.content))
        return httpx.Response(200, json=RESPONSE)

    await make(handler, thinking=False).complete(Request(messages=[{"role": "user", "content": "u"}], tools=[]))
    assert seen[0]["thinking"] == {"type": "disabled"} and "reasoning_effort" not in seen[0]


async def test_http_error_becomes_provider_error():
    p = make(lambda req: httpx.Response(400, json={"error": {"message": "reasoning_content missing"}}))
    with pytest.raises(ProviderError, match="reasoning_content"):
        await p.complete(Request(messages=[{"role": "user", "content": "u"}], tools=[]))


def test_missing_api_key_is_a_clear_error():
    with pytest.raises(ProviderError, match="whim auth set"):
        DeepSeekProvider(ProviderConfig(cache_user_id="character_abc"))


async def test_key_is_loaded_from_credentials_file_and_sent_only_as_bearer():
    credentials.store("deepseek", KEY)
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json=RESPONSE)

    pc = ProviderConfig(cache_user_id="character_abc")
    p = DeepSeekProvider(pc, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    await p.complete(Request(messages=[{"role": "user", "content": "u"}], tools=[]))
    assert seen[0].headers["authorization"] == f"Bearer {KEY}"
    assert KEY not in seen[0].content.decode()  # never in the body / model context
    assert KEY not in repr(p.__dict__)


def test_env_var_overrides_file(monkeypatch):
    credentials.store("deepseek", KEY)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-from-env-override")
    key, source = credentials.lookup("deepseek", "DEEPSEEK_API_KEY")
    assert key.reveal() == "sk-from-env-override" and source == "$DEEPSEEK_API_KEY"


async def test_auth_error_text_is_redacted():
    leaked = {"error": {"message": f"Authentication Fails, your api key: {KEY} is invalid",
                        "type": "authentication_error"}}
    p = make(lambda req: httpx.Response(401, json=leaked))
    with pytest.raises(ProviderError) as ei:
        await p.complete(Request(messages=[{"role": "user", "content": "u"}], tools=[]))
    assert KEY not in str(ei.value) and "[redacted]" in str(ei.value)
    assert ei.value.__cause__ is None and ei.value.__suppress_context__
