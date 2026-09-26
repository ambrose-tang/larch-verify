"""Stage wrappers for worker jobs: differential testing, property testing, mutants."""
from __future__ import annotations

from ..py.mutate import Mutant, generate_mutants
from ..py.runner import WorkerError
from ..spec import FormalSpec


def run_drt(ctx, spec: FormalSpec, *, n: int, source: str | None = None, model_only: bool = False,
            shrink: bool = True, vacuity: bool = False, strategy: str | None = None, seed: int | None = None) -> dict:
    job = ctx.job_base(spec)
    job.update(kind="drt", max_examples=n, model_only=model_only, shrink=shrink, vacuity=vacuity)
    if strategy:
        job["strategy"]["mode"] = strategy
        if strategy == "typed":
            job["edge_cases"] = []
    if seed is not None:
        job["seed"] = seed
    if source is not None:
        job["target"] = dict(job["target"], source=source)
    try:
        return ctx.runner.run(job, timeout=max(300.0, n * ctx.cfg.call_timeout * 0.5))
    except WorkerError as e:
        return {"ok": False, "error": str(e)}


def run_props(ctx, spec: FormalSpec, *, n: int = 300) -> dict:
    job = ctx.job_base(spec)
    job.update(
        kind="props",
        max_examples=n,
        props=[{"name": q.name, "params": [{"name": p.name, "lean_type": p.lean_type} for p in q.params]} for q in spec.active_props()],
    )
    try:
        return ctx.runner.run(job, timeout=600)
    except WorkerError as e:
        return {"ok": False, "error": str(e), "props": {}}


def make_mutants(ctx) -> list[Mutant]:
    return generate_mutants(ctx.info, max_mutants=ctx.cfg.mutants, seed=ctx.cfg.seed)


def run_mutants(ctx, spec: FormalSpec, mutants: list[Mutant]) -> dict:
    job = ctx.job_base(spec)
    job.update(kind="mutants", max_examples=ctx.cfg.mutation_tests,
               mutants=[{"id": m.id, "source": m.module_source} for m in mutants])
    return ctx.runner.run_mutants(job, timeout_per_mutant=30.0)


def run_examples(ctx, spec: FormalSpec, *, model_only: bool = False) -> list[dict]:
    if not spec.examples:
        return []
    job = ctx.job_base(spec)
    job.update(kind="examples", examples=spec.examples, model_only=model_only)
    try:
        return ctx.runner.run(job, timeout=300).get("examples", [])
    except WorkerError:
        return []
