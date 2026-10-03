"""Stages 4-6: turning test disagreements into findings, repairing the model when the
model (not the code) is wrong, and proposing validated fixes as diffs."""
from __future__ import annotations

import difflib
from dataclasses import dataclass

from ..llm.base import UsageLimitError, BudgetExceeded, LLMError, LLMRequest
from ..prompts import (
    ADJUDICATE_SCHEMA, ADJUDICATE_SYSTEM, FIX_SCHEMA, FIX_SYSTEM, MODEL_REPAIR_SCHEMA, formalize_system,
    adjudicate_user, fix_user, model_repair_user,
)
from ..report import Finding, FixProposal
from ..spec import FormalSpec, model_module
from .context import RunContext
from .testing import run_drt

DIVERGENT = ("value", "crash", "timeout", "type")


@dataclass
class Classified:
    confirmed: list[dict]  # impl violates an approved spec (that the model satisfies), or hangs
    unexplained: list[dict]  # differs from model, no spec violated -> adjudicate
    spec_problems: list[dict]  # the model violates a spec too: the spec (or model) is wrong


def representative_failures(drt: dict) -> list[dict]:
    """The shrunk counterexample first, then the rest (deduplicated by kind/specs)."""
    recs = list(drt.get("minimals") or ([drt["minimal"]] if drt.get("minimal") else []))
    recs += drt.get("failures", [])
    seen = set()
    out = []
    for r in recs:
        # Call sequences carry a group (failing operation and outcome classes); functions
        # are grouped by kind and violated specs.
        key = r.get("group") or (r.get("kind"), tuple(sorted(r.get("impl_violates", []))))
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def classify(drt: dict) -> Classified:
    """A spec violation only incriminates the implementation if the model satisfies
    that spec on the same input. If the model violates it too, the spec is wrong (for
    example an over-tight bound introduced to keep it decidable)."""
    confirmed, unexplained, spec_problems = [], [], []
    for r in representative_failures(drt) + list(drt.get("model_violations", [])):
        model_bad = set(r.get("model_violates", []))
        impl_only = [p for p in r.get("impl_violates", []) if p not in model_bad]
        if model_bad:
            spec_problems.append(r)
        if impl_only or r.get("kind") == "timeout":
            confirmed.append(dict(r, impl_violates=impl_only))
        elif r.get("kind") in DIVERGENT:
            unexplained.append(dict(r, impl_violates=[]))
    seen: set = set()
    dedup = []
    for r in confirmed:
        key = (r.get("kind"), tuple(sorted(r.get("impl_violates", []))))
        if key not in seen:
            seen.add(key)
            dedup.append(r)
    return Classified(dedup, unexplained, spec_problems)


def count_kind(drt: dict, rec: dict) -> int:
    return int(drt.get("counts", {}).get(rec.get("kind"), 0))


def adjudicate(ctx: RunContext, spec: FormalSpec, rec: dict) -> tuple[str, str]:
    try:
        resp = ctx.ask(LLMRequest(
            system=ADJUDICATE_SYSTEM, prompt=adjudicate_user(ctx.info, spec, rec), model=ctx.cfg.model,
            stage="adjudicate", effort=ctx.cfg.effort, json_schema=ADJUDICATE_SCHEMA,
        ))
    except UsageLimitError:
        raise
    except (LLMError, BudgetExceeded) as e:
        return "ambiguous", f"(adjudication unavailable: {e})"
    d = resp.data or {}
    return str(d.get("verdict", "ambiguous")), str(d.get("explanation", "")).strip()


SYSTEMATIC_RATE = 0.5  # above this share of disagreeing inputs, look for one common cause first


def systematic(drt: dict, k: int = 8) -> dict | None:
    """When most inputs disagree: the rate, a few diverse examples, and the exception the
    implementation raised on nearly all of them, if it was the same one. None otherwise."""
    from collections import Counter

    c = drt.get("counts", {})
    valid = sum(c.get(x, 0) for x in ("agree",) + DIVERGENT)
    dis = sum(c.get(x, 0) for x in DIVERGENT)
    if valid < 20 or dis < SYSTEMATIC_RATE * valid:
        return None
    fails = list(drt.get("failures") or [])
    seen, samples = set(), []
    for f in sorted(fails, key=lambda f: len(str(f.get("args_repr", "")))):
        key = (str(f.get("impl"))[:60], str(f.get("model"))[:60])
        if key not in seen:
            seen.add(key)
            samples.append(f)
        if len(samples) >= k:
            break
    crashes = [str(f.get("impl", "")) for f in fails if f.get("kind") == "crash"]
    heads = Counter(x.split(":", 1)[0] for x in crashes)
    uniform = None
    if crashes and c.get("crash", 0) >= 0.9 * dis and heads.most_common(1)[0][1] >= 0.9 * len(crashes):
        head = heads.most_common(1)[0][0]
        uniform = next(x for x in crashes if x.startswith(head))[:300]
    return {"rate": dis / valid, "valid": valid, "dis": dis, "samples": samples or fails[:k], "uniform_crash": uniform}


def diagnose_systematic(ctx: RunContext, spec: FormalSpec, diag: dict) -> tuple[str, str]:
    """One judgement over many disagreements: (verdict, common cause and explanation)."""
    from ..prompts import SYSTEMATIC_SCHEMA, SYSTEMATIC_SYSTEM, systematic_user

    try:
        resp = ctx.ask(LLMRequest(system=SYSTEMATIC_SYSTEM, prompt=systematic_user(ctx.info, spec, diag), model=ctx.cfg.model,
                                  stage="adjudicate", effort=ctx.cfg.effort, json_schema=SYSTEMATIC_SCHEMA))
    except UsageLimitError:
        raise
    except (LLMError, BudgetExceeded) as e:
        return "mixed", f"(diagnosis unavailable: {e})"
    d = resp.data or {}
    cause = str(d.get("common_cause", "")).strip()
    why = str(d.get("explanation", "")).strip()
    return str(d.get("verdict", "mixed")), (cause + (" " + why if why else "")).strip()


def repair_model(ctx: RunContext, spec: FormalSpec, issues: list[str]) -> tuple[FormalSpec | None, str, str]:
    """Ask for a corrected model under FIXED approved specs. Returns (new spec or None,
    explanation, spec_conflict)."""
    from .formalize import build_lean, sanity_check

    text = model_module(spec)
    prompt = model_repair_user(ctx.info, spec, text, issues)
    for _ in range(2):
        try:
            resp = ctx.ask(LLMRequest(
                system=formalize_system(ctx.lang.type_guide), prompt=prompt, model=ctx.cfg.model, stage="model-repair",
                effort=ctx.cfg.effort, json_schema=MODEL_REPAIR_SCHEMA,
            ))
        except UsageLimitError:
            raise
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
                system=FIX_SYSTEM, prompt=fix_user(info, spec, recs, feedback, constraints=ctx.lang.fix_constraints(ctx.runner.runtime)),
                model=ctx.cfg.model,
                stage="fix", effort=ctx.cfg.effort, json_schema=FIX_SCHEMA,
            ))
        except UsageLimitError:
            raise
        except (LLMError, BudgetExceeded):
            return None
        d = resp.data or {}
        fixed_fn = str(d.get("fixed_function", "")).strip("\n")
        if not fixed_fn.strip():
            return None
        new_source = ctx.lang.splice_function(info, fixed_fn)
        # The fix must load in the project's own runtime (its version, its installed
        # packages), not just in Larch's.
        chk = ctx.runner.check(new_source)
        if not chk.get("ok"):
            feedback = (
                f"The fixed module does not load in the project's environment ({ctx.runner.runtime.display}): "
                f"{chk.get('error')}. {ctx.lang.fix_constraints(ctx.runner.runtime)}"
            )
            last = FixProposal(explanation=str(d.get("explanation", "")).strip(), diff="", validated=False, validation=feedback)
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
            from ..util import sha256

            return FixProposal(
                explanation=str(d.get("explanation", "")).strip(), diff=diff, validated=True,
                validation=f"agrees with the verified model on {counts.get('agree', 0)} inputs",
                new_source=new_source, base_sha256=sha256(info.module_source),
            )
        if not res.get("ok"):
            feedback = f"Running the fixed function failed: {res.get('error')}"
        else:
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
