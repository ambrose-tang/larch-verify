"""Verification of an HTTP service (a `## service NAME` subject in LARCH.md).

The service is a stateful component whose operations are endpoints: Larch starts a
throwaway database and the service, reads its OpenAPI description and route source,
formalizes it as a Lean state machine, and then runs the component pipeline with an
HTTP implementation (larch/engine/http_impl.py). Mutation analysis and fixes run on a
copy of the project, started on its own port, so the working tree is never modified."""
from __future__ import annotations

import ast
import difflib
import json
import random
import re
import shutil
import tempfile
import time
from datetime import datetime
from pathlib import Path

from ..component import ComponentSpec, Invariant, Operation, StepContract, def_param_names
from ..config import Config
from ..lang import ComponentInfo, Mutant, Runtime, language_for
from ..lean.checker import Checker
from ..lean.toolchain import find_toolchain
from ..lean.workspace import LeanWorkspace
from ..llm.base import LLM, BudgetExceeded, Ledger, LLMError, LLMRequest, UsageLimitError
from ..llm.providers import make_provider
from ..prompts import FIX_SYSTEM, FORMALIZE_REPAIR, SERVICE_SCHEMA, service_system, service_user
from ..report import FixProposal, Report
from ..services import Service, ServiceError, start_service
from ..spec import Param
from ..ui import UI
from ..util import slug, unescape_code
from .component_session import DIVERGENT, _norm, _run, _strip_ns, build, fidelity_problems, fix_accepted, run_seq, sanity
from .context import RunContext
from .formalize import FormalizeError, _code_only
from .runner import JobRunner
from .session import artifacts_root

_SKIP = {".git", ".venv", "venv", "node_modules", "__pycache__", ".larch", ".mypy_cache", ".pytest_cache", "dist", "build", ".tox"}
_ROUTE_DECOR = re.compile(r"^(app|router|api|bp|blueprint)\.(get|post|put|patch|delete|route)$")

SERVICE_FIX_SCHEMA = {
    "type": "object",
    "properties": {
        "explanation": {"type": "string"},
        "file": {"type": "string", "description": "path of the source file to change, as shown"},
        "function": {"type": "string", "description": "name of the handler function to replace"},
        "fixed_function": {"type": "string", "description": "the complete fixed function, with its decorators"},
    },
    "required": ["explanation", "file", "function", "fixed_function"],
    "additionalProperties": False,
}


def verify_service(subject, cfg: Config, ui: UI | None = None, *, llm: LLM | None = None) -> Report:
    ui = ui or UI()
    t_start = time.monotonic()
    name = subject.name
    root = subject.file.parent
    report = Report(function=f"service {name}", file=str(subject.file), config=cfg.to_dict())
    report.kind, report.language, report.line = "service", "http", subject.line
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    artifacts_root(cfg).mkdir(parents=True, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix=f"{stamp}-service-{slug(name, 30)}-", dir=artifacts_root(cfg)))
    report.artifacts_dir = str(run_dir)
    ledger = llm.ledger if llm else Ledger(cfg.budget_usd)
    svc: Service | None = None
    ctx = None
    try:
        llm = llm or LLM(make_provider(cfg.provider, cache=cfg.cache), ledger)
        tc = find_toolchain()
        report.runtime = f"{subject.settings.get('start')} · database: {subject.settings.get('database', 'none')}"
        ui.header(target=f"service {name}", model=f"{cfg.model} via {llm.provider.describe()}", lean=tc.version,
                  runtime=report.runtime)
        with ui.step(f"Start service {name}") as st:
            st.update("starting the database")
            svc = start_service(name, subject.settings, root, run_dir / "service")
            doc = svc.openapi()
            st.done(f"{svc.base_url} · {svc.db.how}" + ("" if doc else " · no OpenAPI description found"))
            report.runtime = f"{subject.settings.get('start')} · {svc.db.how}"
        sources = route_sources(svc.cwd, doc, subject.settings)
        info = _service_info(name, subject, sources, doc)
        ws = LeanWorkspace(run_dir / "lean", tc)
        rt = Runtime(language="http", display="HTTP", executable=svc.base_url, source="service", cmd=[], env={}, load={},
                     int_bound=2**53 - 1)  # integers every JSON client reads exactly
        ctx = RunContext(cfg=cfg, llm=llm, tc=tc, ws=ws, runner=JobRunner(rt, run_dir / "py"), checker=Checker(tc), info=info,
                         ui=ui, run_dir=run_dir, lang=language_for(sources[0][0]) if sources else None,
                         contracts=list(subject.contracts), subject=subject)
        ctx.service = svc
        ctx.service_impl = None  # filled once the operations are known
        ctx.endpoints = render_endpoints(doc) if doc else "(no OpenAPI description: infer the endpoints from the source)"
        ctx.sources = sources
        ctx.hooks = {"formalize": formalize, "mutants": make_mutants, "run_mutants": run_mutants, "fix": propose_fix}
        _run(ctx, report, None)
    except ServiceError as e:
        report.verdict, report.error, report.headline = "error", str(e), f"Could not start service {name}."
    except FormalizeError as e:
        report.verdict = "error"
        report.error = f"{e}: " + "; ".join(" ".join(ln.strip() for ln in p.splitlines()[:4] if ln.strip())[:300] for p in e.problems[:3])
        report.headline = "Could not formalize this service."
    except UsageLimitError:
        raise
    except KeyboardInterrupt:
        report.verdict, report.error, report.headline = "error", "interrupted", "Interrupted."
    except Exception as e:  # noqa: BLE001
        import traceback

        report.verdict, report.error, report.headline = "error", f"{type(e).__name__}: {e}", "Larch hit an internal error."
        (run_dir / "error.txt").write_text(traceback.format_exc())
    finally:
        if svc is not None:
            svc.stop()
    report.elapsed_s = time.monotonic() - t_start
    report.cost_usd = ledger.spent()
    report.nominal_cost_usd = ledger.nominal()
    report.llm_calls = len(ledger.calls)
    report.cost_by_stage = ledger.by_stage()
    if ctx is not None:
        report.stage_seconds = {k: round(v, 2) for k, v in ctx.stage_seconds.items()}
    report.save(run_dir)
    return report


# ---------------------------------------------------------------------------
# What the formalizer sees
# ---------------------------------------------------------------------------

def _schema_str(doc: dict, sch: dict, depth: int = 0) -> str:
    if not isinstance(sch, dict) or depth > 4:
        return "?"
    if "$ref" in sch:
        ref = sch["$ref"].split("/")[-1]
        return _schema_str(doc, doc.get("components", {}).get("schemas", {}).get(ref, {}), depth + 1)
    for key in ("anyOf", "oneOf"):
        if key in sch:
            return " | ".join(_schema_str(doc, s, depth + 1) for s in sch[key])
    t = sch.get("type")
    if t == "object" or "properties" in sch:
        req = set(sch.get("required", []))
        props = ", ".join(f"{k}{'' if k in req else '?'}: {_schema_str(doc, v, depth + 1)}" for k, v in sch.get("properties", {}).items())
        return "{" + props + "}"
    if t == "array":
        return f"[{_schema_str(doc, sch.get('items', {}), depth + 1)}]"
    bounds = "".join(f" {k}={sch[k]}" for k in ("minimum", "exclusiveMinimum", "maximum", "minLength", "maxLength") if k in sch)
    return f"{t or 'any'}{bounds}"


def render_endpoints(doc: dict) -> str:
    lines = []
    for path, ops in sorted((doc.get("paths") or {}).items()):
        for verb, op in ops.items():
            if verb.lower() not in ("get", "post", "put", "patch", "delete") or not isinstance(op, dict):
                continue
            params = ", ".join(f"{p.get('name')} ({p.get('in')}, {_schema_str(doc, p.get('schema', {}))})" for p in op.get("parameters", []))
            body = op.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema")
            resp = "; ".join(f"{code}: {_schema_str(doc, (r.get('content', {}).get('application/json', {}) or {}).get('schema', {}))}"
                             for code, r in (op.get("responses") or {}).items())
            lines.append(f"- {verb.upper()} {path}" + (f" — {op['summary']}" if op.get("summary") else "")
                         + (f"\n  params: {params}" if params else "") + (f"\n  body: {_schema_str(doc, body)}" if body else "")
                         + f"\n  responses: {resp}")
    return "\n".join(lines)


def route_sources(cwd: Path, doc: dict | None, settings: dict, limit: int = 60_000) -> list[tuple[Path, str]]:
    """The files defining the routes: `source:` in LARCH.md, else source files that mention
    the API's paths (or route decorators)."""
    if settings.get("source"):
        files = [(cwd / s.strip()).resolve() for s in settings["source"].split(",") if s.strip()]
    else:
        stems = sorted({re.sub(r"\{[^}]*\}", "", p).rstrip("/") or "/" for p in (doc or {}).get("paths", {})}, key=len, reverse=True)
        files = []
        for f in sorted(cwd.rglob("*")):
            if f.suffix not in (".py", ".js", ".ts", ".mjs", ".cjs") or any(part in _SKIP for part in f.relative_to(cwd).parts) \
                    or "test" in f.name:
                continue
            try:
                text = f.read_text(errors="replace")
            except OSError:
                continue
            if any(f'"{s}' in text or f"'{s}" in text for s in stems if len(s) > 1) or re.search(r"@(app|router)\.(get|post|put|patch|delete)\(", text):
                files.append(f)
    out, total = [], 0
    for f in files:
        try:
            text = f.read_text(errors="replace")
        except OSError:
            continue
        if total + len(text) > limit:
            break
        out.append((f, text))
        total += len(text)
    return out


def _service_info(name: str, subject, sources: list[tuple[Path, str]], doc) -> ComponentInfo:
    main = sources[0][0] if sources else subject.file
    src = "\n\n".join(f"# --- {p.name} ---\n{t}" for p, t in sources)
    return ComponentInfo(path=Path(main), name=f"service_{slug(name, 40)}", source=src, module_source=src, lineno=subject.line,
                         end_lineno=subject.line, col_offset=0, params=[], returns=None, docstring=None, language="python",
                         signature_text=f"service {name}", methods=[])


# ---------------------------------------------------------------------------
# Formalization
# ---------------------------------------------------------------------------

def spec_from_data(ctx: RunContext, data) -> tuple[ComponentSpec | None, list[str]]:
    if not isinstance(data, dict):
        return None, ["the response was not a JSON object"]
    problems: list[str] = []
    ops, http = [], {}
    for o in data.get("operations") or []:
        if not isinstance(o, dict):
            continue
        name = _norm(o.get("name"))
        params, meta = [], []
        for p in o.get("params") or []:
            if isinstance(p, dict):
                params.append(Param(str(p.get("name", "")), str(p.get("lean_type", "")).strip()))
                meta.append({"name": str(p.get("name", "")), "in": str(p.get("in", "body")), "key": str(p.get("key") or p.get("name"))})
        fields = [f for f in o.get("response_fields") or [] if isinstance(f, dict)]
        ret = " × ".join(["Nat"] + [f"Option ({str(f.get('lean_type', '')).strip()})" for f in fields])
        ops.append(Operation(name, params, ret))
        http[name] = {"verb": str(o.get("verb", "GET")).upper(), "path": str(o.get("path", "/")), "params": meta,
                      "fields": [{"key": str(f.get("key", "")), "opaque": bool(f.get("opaque"))} for f in fields],
                      "has_body": any(m["in"] == "body" for m in meta)}
        for m in meta:
            if m["in"] == "path" and "{" + m["key"] + "}" not in http[name]["path"]:
                problems.append(f"operation {name}: path parameter `{m['key']}` does not appear in {http[name]['path']}")
    model = _strip_ns(unescape_code(str(data.get("model", ""))))
    for op in ops:
        # Parameters travel by name (path, query, header, body), so the model's order wins.
        got = def_param_names(model, f"op_{op.method}")
        names = [p.name for p in op.params]
        if got and got[1:] != names and sorted(got[1:]) == sorted(names):
            order = {n: i for i, n in enumerate(got[1:])}
            op.params.sort(key=lambda p: order[p.name])
            http[op.method]["params"].sort(key=lambda m: order[m["name"]])
    contracts = list(ctx.contracts)
    covered: set[int] = set()

    def origin(item: dict, obj) -> None:
        try:
            k = int(item.get("contract", -1))
        except (TypeError, ValueError):
            k = -1
        if 0 <= k < len(contracts):
            covered.add(k)
            obj.origin, obj.contract = "contract", contracts[k].text
            if contracts[k].lean:
                obj.lean = contracts[k].lean

    invs, steps = [], []
    for i in data.get("invariants") or []:
        if isinstance(i, dict):
            inv = Invariant(_norm(i.get("name")), str(i.get("english", "")).strip(), unescape_code(str(i.get("lean", ""))).strip())
            origin(i, inv)
            invs.append(inv)
    for c in data.get("operation_contracts") or []:
        if isinstance(c, dict):
            st = StepContract(_norm(c.get("name")), str(c.get("english", "")).strip(), _norm(c.get("operation")),
                              unescape_code(str(c.get("lean", ""))).strip())
            origin(c, st)
            steps.append(st)
    for k, c in enumerate(contracts):
        if k not in covered:
            problems.append(f"the developer's contract {k} (\"{c.text}\") was not formalized: give it an invariant or an "
                            f"operation contract with \"contract\": {k}")
    try:
        doms = json.loads(data.get("exhaustive_domains") or "{}") or {}
        doms = doms if isinstance(doms, dict) else {}
    except (TypeError, ValueError):
        doms = {}
    spec = ComponentSpec(
        component=ctx.info.name, understanding=str(data.get("understanding", "")).strip(), init_params=[],
        init_pre_english="", init_pre_lean="True", operations=ops, observers=[],
        model_code=model, invariants=invs, steps=steps,
        strategy_code=unescape_code(_code_only(str(data.get("input_generator", "")))),
        exhaustive_domains={k: v for k, v in doms.items() if isinstance(v, list) and v},
        notes=str(data.get("notes", "")), contracts=[c.text for c in contracts], kind="component", http=http,
    )
    problems += spec.validate()
    return spec, problems


def _impl_spec(ctx: RunContext, spec: ComponentSpec, handle=None) -> dict:
    h = handle or ctx.service.handle()
    return {"kind": "http", "service": h.to_json(), "operations": spec.http}


def formalize(ctx: RunContext, *, feedback=None, previous=None, step=None):
    cfg = ctx.cfg
    base = service_user(ctx.subject.name, ctx.endpoints, [(str(p.relative_to(ctx.service.cwd)) if p.is_relative_to(ctx.service.cwd) else str(p), t)
                                                          for p, t in ctx.sources], ctx.contracts)
    prompt = base
    if feedback and previous is not None:
        prompt += f"\n## Reviewer feedback on your previous formalization\n{feedback}\n\nPrevious model:\n```lean\n{previous.model_code}\n```\n"
    type_guide = ctx.lang.type_guide if ctx.lang else ""
    last: list[str] = []
    for attempt in range(cfg.formalize_repairs + 1):
        if step:
            step.update("writing the service model" if attempt == 0 else f"repairing (round {attempt})")
        resp = ctx.ask(LLMRequest(system=service_system(type_guide), prompt=prompt, model=cfg.model, stage="formalize",
                                  effort=cfg.effort, json_schema=SERVICE_SCHEMA))
        spec, problems = spec_from_data(ctx, resp.data)
        san = None
        if spec is not None and not problems:
            ctx.service_impl = _impl_spec(ctx, spec)
            problems = build(ctx, spec)
        if spec is not None and not problems:
            problems, san = sanity(ctx, spec)
        if spec is not None and not problems:
            if step:
                step.update("checking the contracts say what you wrote")
            problems = fidelity_problems(ctx, spec)
            if not problems or attempt == cfg.formalize_repairs:
                ctx.fidelity_warnings = problems  # type: ignore[attr-defined]
                return spec, san, attempt + 1
        last = problems
        prev = json.dumps(resp.data, indent=2, ensure_ascii=False) if isinstance(resp.data, dict) else str(resp.data)
        prompt = base + "\n" + FORMALIZE_REPAIR.format(previous="```json\n" + prev[:14000] + "\n```",
                                                       problems="\n".join(f"- {p}" for p in problems))
    raise FormalizeError("could not produce a consistent model of the service", last)


# ---------------------------------------------------------------------------
# Mutants and fixes: run on a copy of the project
# ---------------------------------------------------------------------------

def _handlers(path: Path) -> list[str]:
    """Route handler functions in a Python source file (decorated routes; without any,
    every top-level function, for frameworks that register routes differently)."""
    try:
        tree = ast.parse(path.read_text())
    except (SyntaxError, OSError):
        return []
    funcs = [n for n in tree.body if isinstance(n, ast.FunctionDef)]
    out = []
    for n in funcs:
        for d in n.decorator_list:
            f = d.func if isinstance(d, ast.Call) else d
            if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and _ROUTE_DECOR.match(f"{f.value.id}.{f.attr}"):
                out.append(n.name)
    return out or [n.name for n in funcs if not n.name.startswith("_")]


def make_mutants(ctx: RunContext, spec: ComponentSpec) -> list[Mutant]:
    from ..py.backend import PYTHON

    pool: list[Mutant] = []
    for path, _ in ctx.sources:
        if path.suffix != ".py":
            continue
        for fn in _handlers(path):
            try:
                info = PYTHON.extract(path, fn)
            except Exception:  # noqa: BLE001
                continue
            for m in PYTHON.generate_mutants(info, max_mutants=8, seed=ctx.cfg.seed):
                m.id = f"{fn}:{m.id}"
                m.description = f"{path.name} {m.description} (in {fn})"
                m.file = str(path)  # type: ignore[attr-defined]
                pool.append(m)
    random.Random(ctx.cfg.seed).shuffle(pool)
    return pool[: ctx.cfg.service_mutants]


def _copy_project(src: Path, dest: Path, exclude: list[Path]) -> None:
    """Copy the project, leaving out dependency/cache folders and Larch's own artifacts
    (which may live inside it)."""
    skip = shutil.ignore_patterns(*_SKIP)
    excluded = {p.resolve() for p in exclude}

    def ignore(d: str, names: list[str]) -> set[str]:
        out = set(skip(d, names))
        return out | {n for n in names if (Path(d) / n).resolve() in excluded}

    shutil.copytree(src, dest, ignore=ignore, symlinks=True)


def _run_variant(ctx: RunContext, spec: ComponentSpec, files: dict[Path, str], *, n: int, exhaustive: bool = False) -> dict:
    """Start a copy of the service with some files replaced, and run sequences against it."""
    svc: Service = ctx.service
    tmp = Path(tempfile.mkdtemp(prefix="variant-", dir=ctx.run_dir))
    copy = tmp / "src"
    _copy_project(svc.cwd, copy, [artifacts_root(ctx.cfg), ctx.run_dir])
    for path, text in files.items():
        (copy / path.relative_to(svc.cwd)).write_text(text)
    variant = None
    try:
        variant = start_service(svc.name, svc.settings, svc.cwd, tmp, cwd_override=copy, database=svc.db, path_from=svc.cwd)
        saved = ctx.service_impl
        ctx.service_impl = _impl_spec(ctx, spec, variant.handle())
        try:
            res = run_seq(ctx, spec, n=n, shrink=False)
            if exhaustive and res.get("ok"):
                exh = run_seq(ctx, spec, n=0, exhaustive=True, shrink=False)
                for k, v in (exh.get("counts") or {}).items():
                    res["counts"][k] = res["counts"].get(k, 0) + v
                res["failures"] = (exh.get("failures") or []) + res.get("failures", [])
        finally:
            ctx.service_impl = saved
        return res
    except ServiceError as e:
        return {"ok": False, "error": str(e), "start_failed": True}
    finally:
        if variant is not None:
            variant.stop(keep_db=True)
        shutil.rmtree(tmp, ignore_errors=True)


def run_mutants(ctx: RunContext, spec: ComponentSpec, mutants: list[Mutant]) -> dict:
    out = []
    for m in mutants:
        res = _run_variant(ctx, spec, {Path(m.file): m.module_source}, n=min(120, ctx.cfg.sequences))  # type: ignore[attr-defined]
        rec = {"id": m.id, "killed": False, "specs": []}
        if res.get("start_failed"):
            rec["invalid"] = True
        elif not res.get("ok"):
            rec["killed"], rec["by"] = True, "crash"
        else:
            c = res.get("counts", {})
            rec["killed"] = any(c.get(k, 0) for k in DIVERGENT)
            rec["likely_equivalent"] = not rec["killed"]
        out.append(rec)
    return {"ok": True, "mutants": out, "valid_inputs": min(120, ctx.cfg.sequences)}


def propose_fix(ctx: RunContext, spec: ComponentSpec, recs: list[dict]) -> FixProposal | None:
    from ..py.backend import PYTHON

    svc: Service = ctx.service
    failing = "\n\n".join(f"```\n{r.get('args_repr')}\n```\nservice: {r.get('impl')}; model: {r.get('model')}" for r in recs)
    srcs = "\n\n".join(f"### {p.relative_to(svc.cwd)}\n```\n{t}\n```" for p, t in ctx.sources)
    contracts = "\n".join(f"- {x.name}: {x.english}" for x in list(spec.active_props()) + list(spec.active_posts()))
    base = (f"## Service source\n{srcs}\n\n## Intended behaviour\n{spec.understanding}\nContracts (proved about the reference "
            f"model):\n{contracts}\n\n## Reference model (Lean)\n```lean\n{spec.model_code}\n```\n\n## Failing request sequences "
            f"(after a fresh database)\n{failing}\n\nFix the handler responsible. Return the file path as shown, the function "
            "name, and the complete fixed function including its decorators. Do not add dependencies.\n")
    feedback, last = None, None
    for _ in range(2):
        try:
            resp = ctx.ask(LLMRequest(system=FIX_SYSTEM, prompt=base + (f"\n## Your previous fix was rejected\n{feedback}\n" if feedback else ""),
                                      model=ctx.cfg.model, stage="fix", effort=ctx.cfg.effort, json_schema=SERVICE_FIX_SCHEMA))
        except UsageLimitError:
            raise
        except (LLMError, BudgetExceeded):
            return last
        d = resp.data or {}
        path = (svc.cwd / str(d.get("file", ""))).resolve()
        if not path.is_file() or not path.is_relative_to(svc.cwd):
            feedback = f"`{d.get('file')}` is not one of the source files shown"
            continue
        try:
            info = PYTHON.extract(path, str(d.get("function", "")))
        except Exception as e:  # noqa: BLE001
            feedback = f"could not find function `{d.get('function')}` in {d.get('file')}: {e}"
            continue
        new_source = PYTHON.splice_function(info, str(d.get("fixed_function", "")))
        diff = "".join(difflib.unified_diff(info.module_source.splitlines(keepends=True), new_source.splitlines(keepends=True),
                                            fromfile=f"a/{path.relative_to(svc.cwd)}", tofile=f"b/{path.relative_to(svc.cwd)}"))
        res = _run_variant(ctx, spec, {path: new_source}, n=max(200, ctx.cfg.sequences // 2), exhaustive=True)
        ok, note, fixes = fix_accepted(res, None, recs, lambda cases: run_seq(ctx, spec, n=0, cases=cases))
        if ok:
            from ..util import sha256

            fix = FixProposal(str(d.get("explanation", "")).strip(), diff, True, note,
                              new_source=new_source, base_sha256=sha256(info.module_source))
            fix.file = str(path)
            fix.fixes = fixes  # type: ignore[attr-defined]
            return fix
        m = (res.get("failures") or [{}])[0]
        feedback = res.get("error") or f"After your fix the service still disagrees with the model:\n```\n{m.get('args_repr')}\n```\n{m.get('impl')} vs {m.get('model')}"
        last = FixProposal(str(d.get("explanation", "")).strip(), diff, False, feedback)
    return last
