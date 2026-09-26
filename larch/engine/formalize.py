"""Stage 1: formalization (model + specs + generator), with a compile/test repair loop."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from ..lean.lint import lint_lean
from ..llm.base import UsageLimitError, LLMRequest
from ..prompts import FORMALIZE_EXAMPLES_ADDENDUM, FORMALIZE_REPAIR, FORMALIZE_SYSTEM, dump, formalize_schema, formalize_user, render_previous
from ..spec import FormalSpec, Param, Postcondition, Property, edge_cases_from_json, examples_from_json, harness_module, model_module
from .context import RunContext
from .testing import run_drt, run_examples, run_props


class FormalizeError(RuntimeError):
    def __init__(self, message: str, problems: list[str] | None = None):
        super().__init__(message)
        self.problems = problems or []


@dataclass
class Sanity:
    valid_inputs: int = 0
    pre_false: int = 0
    domain_errors: int = 0
    impl_violations: list[str] = field(default_factory=list)
    impl_disagreements: int = 0
    warnings: list[str] = field(default_factory=list)
    props: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def spec_from_data(data: dict, ctx: RunContext) -> tuple[FormalSpec | None, list[str]]:
    info = ctx.info
    problems: list[str] = []
    if not isinstance(data, dict):
        return None, ["the response was not a JSON object"]
    raw_params = data.get("params") or []
    expected = [p.lean_name for p in info.params]
    got = [str(p.get("name", "")) for p in raw_params if isinstance(p, dict)]
    if got != expected:
        problems.append(f"`params` must list exactly {expected} in this order (got {got})")
        return None, problems
    params = [
        Param(name=p.lean_name, lean_type=str(rp.get("lean_type", "")).strip(), py_name=p.name)
        for p, rp in zip(info.params, raw_params)
    ]
    posts = [
        Postcondition(name=str(p.get("name", "")).strip(), english=str(p.get("english", "")).strip(), lean=_unescape(str(p.get("lean", ""))).strip())
        for p in data.get("postconditions") or []
        if isinstance(p, dict)
    ]
    props = []
    for q in data.get("properties") or []:
        if not isinstance(q, dict):
            continue
        qps = [Param(name=str(x.get("name", "")), lean_type=str(x.get("lean_type", ""))) for x in q.get("params") or [] if isinstance(x, dict)]
        props.append(Property(name=str(q.get("name", "")).strip(), english=str(q.get("english", "")).strip(), params=qps, lean=str(q.get("lean", "")).strip()))
    _normalize_names(posts, props)
    pre = data.get("precondition") or {}
    spec = FormalSpec(
        function=info.name,
        understanding=str(data.get("understanding", "")).strip(),
        params=params,
        return_type=str(data.get("return_type", "")).strip(),
        exceptions=bool(data.get("exceptions", False)),
        model_code=_strip_namespace(_unescape(str(data.get("model", "")))),
        pre_english=str(pre.get("english", "")).strip(),
        pre_lean=_unescape(str(pre.get("lean", "True"))).strip() or "True",
        postconditions=posts,
        properties=props,
        strategy_code=_unescape(_code_only(str(data.get("input_generator", data.get("strategy", ""))))),
        edge_cases=edge_cases_from_json(str(data.get("edge_cases", ""))),
        notes=str(data.get("notes", "")),
        examples=examples_from_json(str(data.get("documented_examples", ""))),
    )
    problems += spec.validate()
    return spec, problems


_ESCAPED_NL = re.compile(r"\\n(?=[ \t|]|$|def |theorem |abbrev |--|where\b)")


def _unescape(code: str) -> str:
    """Repair prompts show earlier answers as JSON, and models sometimes copy the
    escaping back: a literal backslash-n where a newline was meant. Undo that, without
    touching `\\n` inside Lean string literals (those are followed by a quote)."""
    if "\\n" not in code:
        return code
    return _ESCAPED_NL.sub("\n", code).replace("\\t", "  ")


def _normalize_names(posts: list, props: list) -> None:
    """Spec names are internal Lean identifiers: make them valid, short and unique
    here instead of spending a repair round on them."""
    seen: set[str] = set()
    for item in list(posts) + list(props):
        base = re.sub(r"[^a-z0-9_]+", "_", item.name.lower()).strip("_")
        if not base or not base[0].isalpha():
            base = "spec_" + base if base else "spec"
        base = base[:36].rstrip("_")
        name, k = base, 2
        while name in seen:
            name = f"{base}_{k}"
            k += 1
        seen.add(name)
        item.name = name


def _code_only(text: str) -> str:
    """Strip a markdown fence if the generator came wrapped in one."""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else ""
        t = t.rsplit("```", 1)[0]
    return t.strip("\n")


def _strip_namespace(code: str) -> str:
    """Models sometimes wrap their code in `namespace Larch … end Larch`; Larch adds it."""
    lines = code.strip().splitlines()
    out = [ln for ln in lines if ln.strip() not in ("namespace Larch", "end Larch", "open Larch")]
    return "\n".join(out)


def _numbered(text: str) -> str:
    return "\n".join(f"{i:3d}  {ln}" for i, ln in enumerate(text.splitlines(), 1))


def build_lean(ctx: RunContext, spec: FormalSpec) -> list[str]:
    """Lint + compile the model module and type-check the harness. Returns problems."""
    problems: list[str] = []
    text = model_module(spec)
    for issue in lint_lean(spec.model_code, model_file=True):
        problems.append(f"model code {issue}")
    spec_text = "\n".join([spec.pre_lean] + [p.lean for p in spec.postconditions] + [q.lean for q in spec.properties])
    for issue in lint_lean(spec_text, model_file=True):
        problems.append(f"spec {issue}")
    if problems:
        return problems
    res = ctx.ws.compile_module(ctx.ws.MODEL, text)
    if not res.ok:
        problems.append(
            "Lean rejected the generated LarchModel.lean:\n"
            + res.error_text()
            + "\n\nThe generated file, with line numbers:\n```lean\n" + _numbered(text) + "\n```"
        )
        return problems
    htext = harness_module(spec)
    hres = ctx.ws.check_text(htext, stem="HarnessCheck")
    if not hres.ok:
        err = hres.error_text()
        hint = ""
        if "Decidable" in err or "decide" in err:
            hint = (
                "\nHint: Larch evaluates every spec with `decide`, so each must be decidable. Usual causes: "
                "(1) `match`/`if let` inside a spec: use `result = none`, `result = some v`, `∀ v ∈ result, P v`, "
                "or a Bool-valued helper defined in the model code (`helper … = true`); "
                "(2) unbounded quantifiers: bound them (`∀ i, i < n → …`, `∃ x ∈ xs, …`); "
                "(3) `=` on a type without DecidableEq."
            )
        if "BEq" in err or "ToJson" in err or "FromJson" in err:
            hint += "\nHint: parameter and return types must be built from Int, Nat, Bool, String, Char, List, Array, Option and ×."
        problems.append("The test harness generated from your specs does not compile:\n" + err + hint)
        return problems
    ctx.ws.write(ctx.ws.HARNESS, htext)
    return problems


def sanity_check(ctx: RunContext, spec: FormalSpec) -> tuple[list[str], Sanity]:
    """Cheap testing before approval: does the model satisfy its own specs? Does the
    generator produce valid inputs? (No LLM calls.) Generator-only problems are
    returned in `san.warnings` (fixed by a cheap targeted repair, never blocking)."""
    san = Sanity()
    problems: list[str] = []
    gen_problems: list[str] = san.warnings
    res = run_drt(ctx, spec, n=min(1000, ctx.cfg.tests), model_only=True, shrink=True)
    if not res.get("ok"):
        return [f"testing the model failed: {res.get('error')}"], san
    counts = res.get("counts", {})
    san.valid_inputs = counts.get("model_only", 0)
    san.pre_false = counts.get("pre_false", 0)
    san.domain_errors = counts.get("domain", 0)
    total = max(1, res.get("inputs", 0))
    if res.get("strategy_note"):
        gen_problems.append(res["strategy_note"])
    if counts.get("harness_error"):
        errs = [f.get("error") for f in res.get("failures", []) if f.get("kind") == "harness_error"][:2]
        problems.append(f"evaluating the model failed on some inputs: {errs}")
    if res.get("slow_model_inputs") or counts.get("model_timeout", 0) > total * 0.05:
        ex = "; ".join(f"({a})" for a in res.get("slow_model_inputs", [])) or "several inputs"
        problems.append(
            f"evaluating the model and its specs takes more than 3 seconds on {ex}. Everything must be "
            "cheap to evaluate on inputs up to ~2^64: no recursion or iteration proportional to an "
            "integer's magnitude in the model (use Nat.gcd, Nat.sqrt, closed forms, list recursion), and no "
            "spec quantifier ranging over values up to an input's magnitude (e.g. 'every d ≤ |a|'); "
            "state such properties differently or bound the inputs in the precondition"
        )
    mv = res.get("minimal_model_violation") or (res.get("model_violations") or [None])[0]
    if mv:
        problems.append(
            f"The model violates its own spec(s) {mv.get('model_violates')} on input "
            f"({mv.get('args_repr')}): model returned {mv.get('model')}. Either the model or the spec is wrong."
        )
    if ctx.cfg.test_strategy in ("llm", "mixed") and spec.strategy_code:
        if san.domain_errors > total * 0.2:
            gen_problems.append(f"the input generator produced {san.domain_errors}/{total} values that do not fit the declared Lean parameter types")
        valid_frac = san.valid_inputs / total
        if valid_frac < 0.25 and spec.pre_lean.strip() != "True":
            gen_problems.append(
                f"only {san.valid_inputs}/{total} generated inputs satisfy the precondition; the generator should "
                "produce mostly valid inputs"
            )
    if san.valid_inputs == 0 and not problems:
        problems.append("no generated input satisfies the precondition: it may be unsatisfiable")
    for ex in run_examples(ctx, spec, model_only=True):
        if ex.get("model_ok") is False:
            problems.append(
                f"The model contradicts the documented example {ctx.info.name}({ex['args_repr']}) = {ex['expected']}: "
                f"the model returns {ex.get('model')}. The documented examples are ground truth."
            )
    if spec.active_props():
        pres = run_props(ctx, spec, n=200)
        san.props = pres.get("props", {})
        for name, r in san.props.items():
            if not r.get("holds", True):
                problems.append(f"Property `{name}` is false for the model: counterexample ({r.get('counterexample')}).")
            elif r.get("tested", 0) == 0:
                problems.append(f"Property `{name}` could not be evaluated on any generated input (check its parameter types)")
    if problems:
        return problems, san
    # Informational: does the implementation already disagree? (Shown during review.)
    impl = run_drt(ctx, spec, n=200, shrink=False)
    if impl.get("ok"):
        viol: set[str] = set()
        for f in impl.get("failures", []):
            viol.update(f.get("impl_violates", []))
        san.impl_violations = sorted(viol)
        c = impl.get("counts", {})
        san.impl_disagreements = sum(c.get(k, 0) for k in ("value", "crash", "timeout", "type"))
    return problems, san


_BAD_SPEC_PATTERNS = [
    re.compile(r"Larch\.(?:post|prop)_([a-z][a-z0-9_]*)"),
    re.compile(r"violates its own spec\(s\) \[([^\]]*)\]"),
    re.compile(r"Property `([a-z][a-z0-9_]*)`"),
    re.compile(r"spec `?([a-z][a-z0-9_]*)`? (?:is|was)"),
]


def _quarantine(ctx: RunContext, spec: FormalSpec, problems: list[str], step=None):
    """Repair budget exhausted. If every remaining problem is attributable to specific
    specs, drop those specs (with a warning) rather than failing the whole run."""
    names = set(spec.spec_names())
    bad: set[str] = set()
    for p in problems:
        found = set()
        for pat in _BAD_SPEC_PATTERNS:
            for m in pat.finditer(p):
                for n in re.findall(r"[a-z][a-z0-9_]*", m.group(1)):
                    if n in names:
                        found.add(n)
        if not found:
            return None  # a problem not tied to a spec (e.g. the model itself): cannot rescue
        bad |= found
    keep = names - bad
    if not bad or not keep:
        return None
    trimmed = FormalSpec.from_json(spec.to_json())
    trimmed.postconditions = [p for p in trimmed.postconditions if p.name in keep]
    trimmed.properties = [q for q in trimmed.properties if q.name in keep]
    if step:
        step.update(f"dropping {len(bad)} inconsistent spec(s)")
    if build_lean(ctx, trimmed):
        return None
    problems2, san = sanity_check(ctx, trimmed)
    if problems2:
        return None
    san.warnings = list(san.warnings) + [
        f"dropped spec `{n}`: it could not be made consistent with the model (it was false or not checkable)"
        for n in sorted(bad)
    ]
    return trimmed, san


def _repair_generator(ctx: RunContext, spec: FormalSpec, san: Sanity) -> tuple[FormalSpec, Sanity]:
    """One cheap, targeted call to fix only the input generator. If it does not help,
    keep the original (DRT then relies on the type-directed half of the mix)."""
    from ..prompts import STRATEGY_SCHEMA, STRATEGY_SYSTEM, strategy_user

    try:
        resp = ctx.ask(LLMRequest(
            system=STRATEGY_SYSTEM, prompt=strategy_user(ctx.info, spec, spec.strategy_code, "; ".join(san.warnings)),
            model=ctx.cfg.model, stage="generator-repair", effort="low", json_schema=STRATEGY_SCHEMA,
        ))
    except UsageLimitError:
        raise
    except Exception:  # noqa: BLE001 - generator quality is not worth failing the run
        return spec, san
    code = _code_only(str((resp.data or {}).get("input_generator", "")))
    if not code.strip():
        return spec, san
    candidate = FormalSpec.from_json(spec.to_json())
    candidate.strategy_code = code
    problems, san2 = sanity_check(ctx, candidate)
    if not problems and len(san2.warnings) < len(san.warnings) + (0 if san.warnings else 1):
        if not san2.warnings or san2.valid_inputs > san.valid_inputs:
            return candidate, san2
    return spec, san


def effective_mode(ctx: RunContext) -> str:
    """`auto`: write the model from the documentation alone when there is real
    documentation (so bugs in the code cannot leak into the model), otherwise from
    the documentation and the code."""
    mode = ctx.cfg.formalize_mode
    if mode != "auto":
        return mode
    doc = ctx.info.docstring or ""
    return "intent" if len(doc.split()) >= 8 else "hybrid"


def formalize(ctx: RunContext, *, feedback: str | None = None, previous: FormalSpec | None = None, step=None) -> tuple[FormalSpec, Sanity, int]:
    cfg = ctx.cfg
    base_prompt = formalize_user(ctx.info, effective_mode(ctx))
    prompt = base_prompt
    if feedback and previous is not None:
        prompt += (
            "\n## Reviewer feedback on your previous formalization\n"
            f"{feedback}\n\nPrevious formalization:\n```json\n{dump(_to_llm_json(previous))}\n```\n"
            "Revise it to address the feedback."
        )
    rounds = 0
    data = None
    last_problems: list[str] = []
    for attempt in range(cfg.formalize_repairs + 1):
        rounds += 1
        if step:
            step.update("writing Lean model and specs" if attempt == 0 else f"repairing (round {attempt})")
        resp = ctx.ask(LLMRequest(
            system=FORMALIZE_SYSTEM + (FORMALIZE_EXAMPLES_ADDENDUM if cfg.doc_examples else ""),
            prompt=prompt, model=cfg.model, stage="formalize",
            effort=cfg.effort, json_schema=formalize_schema(cfg.doc_examples),
        ))
        data = resp.data
        spec, problems = spec_from_data(data, ctx)
        san = Sanity()
        if spec is not None and not problems:
            if step:
                step.update("compiling Lean model")
            problems = build_lean(ctx, spec)
        if spec is not None and not problems:
            if step:
                step.update("testing model against its specs")
            problems, san = sanity_check(ctx, spec)
        if spec is not None and not problems:
            if san.warnings and ctx.cfg.test_strategy in ("llm", "mixed"):
                if step:
                    step.update("repairing the input generator")
                spec, san = _repair_generator(ctx, spec, san)
            return spec, san, rounds
        last_problems = problems
        if attempt == cfg.formalize_repairs and spec is not None:
            rescued = _quarantine(ctx, spec, problems, step)
            if rescued is not None:
                return rescued[0], rescued[1], rounds
        prompt = base_prompt + "\n" + FORMALIZE_REPAIR.format(
            previous=render_previous(data), problems="\n".join(f"- {p}" for p in problems)
        )
    raise FormalizeError("could not produce a consistent Lean model and specs", last_problems)


def _to_llm_json(spec: FormalSpec) -> dict:
    return {
        "understanding": spec.understanding,
        "params": [{"name": p.name, "lean_type": p.lean_type} for p in spec.params],
        "return_type": spec.return_type,
        "exceptions": spec.exceptions,
        "model": spec.model_code,
        "precondition": {"english": spec.pre_english, "lean": spec.pre_lean},
        "postconditions": [{"name": p.name, "english": p.english, "lean": p.lean} for p in spec.postconditions],
        "properties": [
            {"name": q.name, "english": q.english, "params": [{"name": x.name, "lean_type": x.lean_type} for x in q.params], "lean": q.lean}
            for q in spec.properties
        ],
        "input_generator": spec.strategy_code,
        "edge_cases": json.dumps(spec.edge_cases),
        "notes": spec.notes,
    }
