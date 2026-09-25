"""Provider-neutral LLM request/response types, cost ledger and budget."""
from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


class LLMError(RuntimeError):
    pass


class BudgetExceeded(LLMError):
    pass


class UsageLimitError(LLMError):
    """The account's usage/session limit is exhausted (not a transient rate limit)."""

    def __init__(self, message: str, reset_hint: str = ""):
        super().__init__(message)
        self.reset_hint = reset_hint


@dataclass
class LLMRequest:
    system: str
    prompt: str
    model: str
    stage: str = ""
    max_tokens: int = 32000
    effort: str | None = None  # low | medium | high | xhigh | max
    json_schema: dict | None = None


@dataclass
class LLMResponse:
    text: str
    model: str
    data: Any = None  # parsed JSON when a schema was requested
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    latency_s: float = 0.0
    cached: bool = False  # served from Larch's response cache (no spend)
    stop_reason: str | None = None


class Provider(ABC):
    name: str = "provider"

    @abstractmethod
    def complete(self, req: LLMRequest) -> LLMResponse: ...

    def describe(self) -> str:
        return self.name


@dataclass
class CallRecord:
    stage: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float  # nominal cost (what the call cost when it was made)
    spent_usd: float  # actual spend in this run (0 for cache hits)
    latency_s: float
    cached: bool
    t: float = field(default_factory=time.time)


class Ledger:
    """Thread-safe accounting of LLM usage for one run, with an optional budget."""

    def __init__(self, budget_usd: float | None = None):
        self.budget_usd = budget_usd
        self.calls: list[CallRecord] = []
        self._lock = threading.Lock()

    def check_budget(self) -> None:
        if self.budget_usd is not None and self.spent() >= self.budget_usd:
            raise BudgetExceeded(f"LLM budget of ${self.budget_usd:.2f} exhausted")

    def record(self, stage: str, resp: LLMResponse) -> None:
        with self._lock:
            self.calls.append(
                CallRecord(
                    stage=stage,
                    model=resp.model,
                    input_tokens=resp.input_tokens + resp.cache_read_tokens + resp.cache_write_tokens,
                    output_tokens=resp.output_tokens,
                    cost_usd=resp.cost_usd,
                    spent_usd=0.0 if resp.cached else resp.cost_usd,
                    latency_s=resp.latency_s,
                    cached=resp.cached,
                )
            )

    def spent(self) -> float:
        with self._lock:
            return sum(c.spent_usd for c in self.calls)

    def nominal(self) -> float:
        with self._lock:
            return sum(c.cost_usd for c in self.calls)

    def llm_seconds(self) -> float:
        with self._lock:
            return sum(c.latency_s for c in self.calls)

    def by_stage(self) -> dict[str, dict[str, float]]:
        out: dict[str, dict[str, float]] = {}
        with self._lock:
            for c in self.calls:
                d = out.setdefault(c.stage or "other", {"calls": 0, "cost_usd": 0.0, "input_tokens": 0, "output_tokens": 0})
                d["calls"] += 1
                d["cost_usd"] += c.cost_usd
                d["input_tokens"] += c.input_tokens
                d["output_tokens"] += c.output_tokens
        return out


class LLM:
    """What pipeline stages use: a provider plus accounting and budget checks."""

    def __init__(self, provider: Provider, ledger: Ledger | None = None):
        self.provider = provider
        self.ledger = ledger or Ledger()

    def complete(self, req: LLMRequest) -> LLMResponse:
        self.ledger.check_budget()
        resp = self.provider.complete(req)
        self.ledger.record(req.stage, resp)
        return resp
