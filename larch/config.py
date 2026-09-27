"""Run configuration. Defaults are the winners of the benchmark in EVALS.md."""
from __future__ import annotations

import os
import tomllib
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path


@dataclass
class Config:
    # LLM
    provider: str = "auto"  # auto | anthropic | claude-code
    model: str = "claude-sonnet-5"  # formalization / adjudication / fixes
    prover_model: str | None = None  # defaults to `model`
    effort: str | None = "low"
    cache: bool = False
    budget_usd: float = 5.0

    # Approaches (see EVALS.md for the comparison that picked these)
    formalize_mode: str = "auto"  # auto (intent if documented, else hybrid) | hybrid | intent | transliterate
    doc_examples: bool = True  # extract documented examples and check model + implementation against them
    proof_strategy: str = "portfolio+llm"  # llm | portfolio+llm | portfolio+sketch
    test_strategy: str = "mixed"  # typed | llm | mixed

    # Effort knobs
    proof_attempts: int = 4
    prover_efforts: str = "low"  # per-attempt effort schedule, e.g. "medium,medium,high,high" (empty: `effort`)
    proof_cache: bool = True  # reuse proofs of identical (model, spec) pairs; always re-checked
    proof_budget_usd: float = 0.75  # stop working on one spec once its LLM calls cost this much
    formalize_repairs: int = 3
    model_repairs: int = 2
    tests: int = 2000
    mutants: int = 40
    mutation_tests: int = 300
    call_timeout: float = 1.0
    parallel: int = 4
    seed: int = 0

    # Behaviour
    auto_approve: bool = False
    adjudicate: bool = True
    propose_fixes: bool = True
    run_mutation: bool = True
    run_proofs: bool = True
    python: str | None = None
    artifacts: str | None = None  # directory for run artifacts (default: user cache)

    extra: dict = field(default_factory=dict)

    @property
    def prover(self) -> str:
        return self.prover_model or self.model

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def load(cls, project_dir: Path | None = None, **overrides) -> "Config":
        """Defaults < ~/.config/larch/config.toml < [tool.larch] in pyproject.toml <
        .larch.toml < environment (LARCH_*) < explicit overrides (CLI flags)."""
        cfg = cls()
        names = {f.name for f in fields(cls)}
        sources: list[dict] = []
        user = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "larch" / "config.toml"
        if user.exists():
            sources.append(_read_toml(user))
        if project_dir is not None:
            pp = project_dir / "pyproject.toml"
            if pp.exists():
                sources.append(_read_toml(pp).get("tool", {}).get("larch", {}))
            lt = project_dir / ".larch.toml"
            if lt.exists():
                sources.append(_read_toml(lt))
        env = {}
        for f in fields(cls):
            v = os.environ.get("LARCH_" + f.name.upper())
            if v is not None:
                env[f.name] = v
        sources.append(env)
        sources.append({k: v for k, v in overrides.items() if v is not None})
        for src in sources:
            for k, v in src.items():
                k = k.replace("-", "_")
                if k in names:
                    setattr(cfg, k, _coerce(getattr(cls(), k), v))
        return cfg


def _read_toml(p: Path) -> dict:
    try:
        return tomllib.loads(p.read_text())
    except (tomllib.TOMLDecodeError, OSError):
        return {}


def _coerce(default, value):
    if isinstance(value, str):
        if isinstance(default, bool):
            return value.lower() in ("1", "true", "yes", "on")
        if isinstance(default, int) and not isinstance(default, bool):
            return int(value)
        if isinstance(default, float):
            return float(value)
    return value
