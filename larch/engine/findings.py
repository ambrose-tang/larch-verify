"""Stages 4-6: turning test disagreements into findings, repairing the model when the
model (not the code) is wrong, and proposing validated fixes as diffs."""
from __future__ import annotations

import difflib
from dataclasses import dataclass

from ..llm.base import BudgetExceeded, LLMError, LLMRequest
from ..prompts import (
    ADJUDICATE_SCHEMA, ADJUDICATE_SYSTEM, FIX_SCHEMA, FIX_SYSTEM, FORMALIZE_SYSTEM, MODEL_REPAIR_SCHEMA,
    adjudicate_user, fix_user, model_repair_user,
)
from ..py.extract import splice_function
from ..report import Finding, FixProposal
from ..spec import FormalSpec, model_module
from .context import RunContext
from .testing import run_drt

DIVERGENT = ("value", "crash", "timeout", "type")


@dataclass
class Classified:
    confirmed: list[dict]  # impl violates an approved spec, or hangs
    unexplained: list[dict]  # differs from model, no spec violated -> adjudicate


def representative_failures(drt: dict) -> list[dict]:
    """The shrunk counterexample first, then the rest (deduplicated by kind/specs)."""
    recs = []
    if drt.get("minimal"):
        recs.append(drt["minimal"])
    recs += drt.get("failures", [])
    seen = set()
    out = []
    for r in recs:
        key = (r.get("kind"), tuple(sorted(r.get("impl_violates", []))))
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def classify(drt: dict) -> Classified:
    confirmed, unexplained = [], []
    for r in representative_failures(drt):
        if r.get("impl_violates") or r.get("kind") == "timeout":
            confirmed.append(r)
        elif r.get("kind") in DIVERGENT:
            unexplained.append(r)
    return Classified(confirmed, unexplained)


def count_kind(drt: dict, rec: dict) -> int:
    return int(drt.get("counts", {}).get(rec.get("kind"), 0))


def adjudicate(ctx: RunContext, spec: FormalSpec, rec: dict) -> tuple[str, str]:
    try:
        resp = ctx.ask(LLMRequest(
            system=ADJUDICATE_SYSTEM, prompt=adjudicate_user(ctx.info, spec, rec), model=ctx.cfg.model,
            stage="adjudicate", effort=ctx.cfg.effort, json_schema=ADJUDICATE_SCHEMA,
        ))
    except (LLMError, BudgetExceeded) as e:
        return "ambiguous", f"(adjudication unavailable: {e})"
    d = resp.data or {}
    return str(d.get("verdict", "ambiguous")), str(d.get("explanation", "")).strip()


def repair_model(ctx: RunContext, spec: FormalSpec, issues: list[str]) -> tuple[FormalSpec | None, str, str]:
    """Ask for a corrected model under FIXED approved specs. Returns (new spec or None,
    explanation, spec_conflict)."""
    from .formalize import build_lean, sanity_check

    text = model_module(spec)
    prompt = model_repair_user(ctx.info, spec, text, issues)
    for _ in range(2):
        try:
            resp = ctx.ask(LLMRequest(
                system=FORMALIZE_SYSTEM, prompt=prompt, model=ctx.cfg.model, stage="model-repair",
                effort=ctx.cfg.effort, json_schema=MODEL_REPAIR_SCHEMA,
            ))
        except (LLMError, BudgetExceeded) as e:
            return None, str(e), ""
        d = resp.data or {}
        new = FormalSpec.from_json(spec.to_json())
        new.model_code = str(d.get("model", "")).strip()
        problems = new.validate() or build_lean(ctx, new)
        if not problems:
            problems, _ = sanity_check(ctx, new)
        if not problems:
            return new, str(d.get("explanation", "")), str(d.get("spec_conflict", ""))
        prompt += "\n\n## Your previous repair was rejected\n" + "\n".join(f"- {p}" for p in problems)
    # Restore the working model files.
    build_lean(ctx, spec)
    return None, "model repair did not produce a consistent model", ""


def propose_fix(ctx: RunContext, spec: FormalSpec, recs: list[dict]) -> FixProposal | None:
    info = ctx.info
    feedback = None
    for attempt in range(2):
        try:
            resp = ctx.ask(LLMRequest(
                system=FIX_SYSTEM, prompt=fix_user(info, spec, recs, feedback), model=ctx.cfg.model,
                stage="fix", effort=ctx.cfg.effort, json_schema=FIX_SCHEMA,
            ))
        except (LLMError, BudgetExceeded):
            return None
        d = resp.data or {}
        fixed_fn = str(d.get("fixed_function", "")).strip("\n")
        if not fixed_fn.strip():
            return None
        new_source = splice_function(info, fixed_fn)
        try:
            compile(new_source, str(info.path), "exec")
        except SyntaxError as e:
            feedback = f"The fixed function does not parse: {e}"
            continue
        # Validate: the patched implementation must agree with the verified model.
        res = run_drt(ctx, spec, n=max(500, ctx.cfg.tests // 2), source=new_source, shrink=True, seed=ctx.cfg.seed + 7)
        counts = res.get("counts", {})
        bad = sum(counts.get(k, 0) for k in DIVERGENT) + sum(1 for f in res.get("failures", []) if f.get("impl_violates"))
        diff = "".join(difflib.unified_diff(
            info.module_source.splitlines(keepends=True), new_source.splitlines(keepends=True),
            fromfile=f"a/{info.path.name}", tofile=f"b/{info.path.name}",
        ))
        if res.get("ok") and bad == 0 and counts.get("agree", 0) > 0:
            return FixProposal(
                explanation=str(d.get("explanation", "")).strip(), diff=diff, validated=True,
                validation=f"agrees with the verified model on {counts.get('agree', 0)} inputs",
            )
        m = res.get("minimal") or (res.get("failures") or [{}])[0]
        feedback = (
            f"After your fix, the function still disagrees with the reference model: "
            f"input ({m.get('args_repr')}) returned {m.get('impl')}, expected {m.get('model')}."
        )
        last = FixProposal(explanation=str(d.get("explanation", "")).strip(), diff=diff, validated=False, validation=feedback)
    return last if "last" in locals() else None


def make_finding(rec: dict, kind: str, confidence: str, explanation: str = "", count: int = 1) -> Finding:
    return Finding(
        kind=kind,
        confidence=confidence,
        args_repr=str(rec.get("args_repr", "")),
        impl=str(rec.get("impl", "")),
        model=str(rec.get("model", "")),
        violated_specs=list(rec.get("impl_violates", [])),
        explanation=explanation or str(rec.get("detail", "")),
        count=count,
    )
