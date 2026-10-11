from whim.config import ProviderConfig
from whim.provider.base import Completion, Provider, ProviderError, Request, ToolCall


def from_config(cfg: ProviderConfig) -> Provider:
    if cfg.kind == "deepseek":
        from whim.provider.deepseek import DeepSeekProvider

        return DeepSeekProvider(cfg)
    if cfg.kind == "script":
        from whim.provider.fake import RuleScriptProvider

        return RuleScriptProvider(cfg.script)
    raise ProviderError(f"unknown provider kind {cfg.kind!r}")


__all__ = ["Completion", "Provider", "ProviderError", "Request", "ToolCall", "from_config"]
