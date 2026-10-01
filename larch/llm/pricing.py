"""List prices (USD per million tokens) for cost accounting with the direct API.

The Claude Code CLI provider reports cost itself; these are used for the
Anthropic API provider. Cache writes are billed at 1.25x input (5-minute TTL) and
cache reads at 0.1x input.
"""
from __future__ import annotations

PRICES: dict[str, tuple[float, float]] = {
    "claude-fable-5-1": (10.0, 50.0),
    "claude-fable-5": (10.0, 50.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}

ALIASES = {
    "opus": "claude-opus-5",
    "sonnet": "claude-sonnet-5",
    "haiku": "claude-haiku-4-5",
    "fable": "claude-fable-5-1",
}


def resolve_model(name: str) -> str:
    return ALIASES.get(name, name)


def price_for(model: str) -> tuple[float, float]:
    m = resolve_model(model)
    for prefix in ("global.", "us.", "eu.", "apac.", "anthropic."):  # Bedrock IDs
        if m.startswith(prefix):
            m = m[len(prefix):]
    if m in PRICES:
        return PRICES[m]
    for key, val in PRICES.items():
        if m.startswith(key):
            return val
    return PRICES["claude-opus-5"]


def cost_usd(model: str, input_tokens: int, output_tokens: int, cache_read: int = 0, cache_write: int = 0) -> float:
    pin, pout = price_for(model)
    return (input_tokens * pin + cache_write * pin * 1.25 + cache_read * pin * 0.1 + output_tokens * pout) / 1e6


def supports_effort(model: str) -> bool:
    return not resolve_model(model).startswith("claude-haiku")


def supports_adaptive_thinking(model: str) -> bool:
    return not resolve_model(model).startswith("claude-haiku")
