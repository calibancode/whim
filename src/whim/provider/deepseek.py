"""First-party DeepSeek Chat Completions adapter.

Verified against api-docs.deepseek.com (2026-10-10):
- `user_id` is a top-level body field ([a-zA-Z0-9-_], <= 512 chars); sent on every request.
- Thinking mode is on by default; with `tools` present, every earlier assistant
  message's `reasoning_content` must be passed back or the API returns 400. The
  harness persists it per assistant message and replays it verbatim.
- `tool_choice: required` is rejected in thinking mode, so we leave tool_choice unset.
- Usage reports `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens`.
"""

from __future__ import annotations

import time

import openai

from whim import credentials
from whim.config import ProviderConfig
from whim.credentials import Secret
from whim.provider.base import Completion, ProviderError, Request, ToolCall


class DeepSeekProvider:
    name = "deepseek"

    def __init__(self, cfg: ProviderConfig, *, api_key: Secret | None = None, http_client=None):
        if api_key is None:
            try:
                found = credentials.lookup("deepseek", cfg.api_key_env)
            except credentials.CredentialError as e:
                raise ProviderError(str(e)) from None
            if found is None:
                raise ProviderError(
                    "no DeepSeek API key configured; run `whim auth set` "
                    f"(or set ${cfg.api_key_env} as an override)"
                )
            api_key = found[0]
        self.cfg = cfg
        self._key = api_key
        self.client = openai.AsyncOpenAI(
            api_key=api_key.reveal(), base_url=cfg.base_url, timeout=cfg.timeout_seconds, max_retries=2,
            http_client=http_client,
        )

    def body_extras(self) -> dict:
        return {
            "user_id": self.cfg.cache_user_id,
            "thinking": {"type": "enabled" if self.cfg.thinking else "disabled"},
        }

    async def complete(self, request: Request) -> Completion:
        kwargs: dict = {
            "model": self.cfg.model,
            "messages": request.messages,
            "extra_body": self.body_extras(),
        }
        if request.tools:
            kwargs["tools"] = request.tools
        if self.cfg.thinking and self.cfg.reasoning_effort:
            kwargs["reasoning_effort"] = self.cfg.reasoning_effort
        if request.max_tokens:
            kwargs["max_tokens"] = request.max_tokens
        started = time.monotonic()
        try:
            resp = await self.client.chat.completions.create(**kwargs)
        except openai.OpenAIError as e:
            # Error text reaches logs and the ledger; strip the key and anything key-shaped,
            # and drop the chained exception, whose message is unredacted.
            raise ProviderError(credentials.redact(f"{type(e).__name__}: {e}", self._key)) from None
        latency_ms = int((time.monotonic() - started) * 1000)
        choice = resp.choices[0]
        msg = choice.message
        extra = msg.model_extra or {}
        usage = resp.usage
        u: dict = {"latency_ms": latency_ms}
        if usage is not None:
            ux = usage.model_extra or {}
            u.update(
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
                cache_hit_tokens=ux.get("prompt_cache_hit_tokens"),
                cache_miss_tokens=ux.get("prompt_cache_miss_tokens"),
            )
            details = usage.completion_tokens_details
            if details is not None and details.reasoning_tokens is not None:
                u["reasoning_tokens"] = details.reasoning_tokens
        return Completion(
            content=msg.content,
            reasoning_content=extra.get("reasoning_content"),
            tool_calls=[
                ToolCall(id=tc.id, name=tc.function.name, arguments=tc.function.arguments or "")
                for tc in (msg.tool_calls or [])
            ],
            finish_reason=choice.finish_reason,
            model=resp.model,
            usage=u,
        )

    async def close(self) -> None:
        await self.client.close()
