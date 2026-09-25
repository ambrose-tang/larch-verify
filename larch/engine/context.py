"""Everything a pipeline stage needs, bundled."""
from __future__ import annotations

import itertools
import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Config
from ..lean.checker import Checker
from ..lean.toolchain import LeanToolchain
from ..lean.workspace import LeanWorkspace
from ..llm.base import LLM, LLMRequest, LLMResponse
from ..py.extract import FunctionInfo
from ..py.runner import PythonRunner
from ..ui import UI


@dataclass
class RunContext:
    cfg: Config
    llm: LLM
    tc: LeanToolchain
    ws: LeanWorkspace
    runner: PythonRunner
    checker: Checker
    info: FunctionInfo
    ui: UI
    run_dir: Path
    stage_seconds: dict = field(default_factory=dict)
    _n: itertools.count = field(default_factory=lambda: itertools.count(1))
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def ask(self, req: LLMRequest) -> LLMResponse:
        """LLM call with a transcript written to the run directory (auditability)."""
        with self._lock:
            n = next(self._n)
        t0 = time.monotonic()
        try:
            resp = self.llm.complete(req)
        except Exception as e:
            self._log(n, req, None, error=str(e))
            raise
        self._log(n, req, resp, elapsed=time.monotonic() - t0)
        return resp

    def _log(self, n: int, req: LLMRequest, resp: LLMResponse | None, error: str | None = None, elapsed: float = 0.0) -> None:
        d = self.run_dir / "llm"
        d.mkdir(parents=True, exist_ok=True)
        meta = {
            "stage": req.stage,
            "model": req.model,
            "effort": req.effort,
            "cost_usd": resp.cost_usd if resp else 0,
            "cached": resp.cached if resp else False,
            "latency_s": round(elapsed, 2),
            "error": error,
        }
        body = [f"# {n:03d} {req.stage}", "```json", json.dumps(meta, indent=2), "```", "## Prompt", req.prompt]
        if resp is not None:
            body += ["## Response", resp.text if not resp.data else json.dumps(resp.data, indent=2, ensure_ascii=False)]
        (d / f"{n:03d}_{req.stage or 'call'}.md").write_text("\n\n".join(body))

    def timed(self, stage: str):
        ctx = self

        class _T:
            def __enter__(self_inner):
                self_inner.t0 = time.monotonic()

            def __exit__(self_inner, *exc):
                ctx.stage_seconds[stage] = ctx.stage_seconds.get(stage, 0.0) + time.monotonic() - self_inner.t0

        return _T()

    # -- worker job scaffolding -----------------------------------------------------------
    def job_base(self, spec) -> dict:
        return {
            "spec": {
                "params": [{"name": p.name, "lean_type": p.lean_type} for p in spec.params],
                "return_type": spec.return_type,
                "exceptions": spec.exceptions,
                "posts": [p.name for p in spec.active_posts()],
            },
            "harness": {"cmd": self.ws.harness_cmd(), "env": self.ws.harness_env(), "cwd": str(self.ws.root)},
            "target": {"path": str(self.info.path), "function": self.info.name},
            "strategy": {"mode": self.cfg.test_strategy, "code": spec.strategy_code},
            "edge_cases": spec.edge_cases if self.cfg.test_strategy != "typed" else [],
            "seed": self.cfg.seed,
            "call_timeout": self.cfg.call_timeout,
        }
