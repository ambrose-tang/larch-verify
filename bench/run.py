"""Run Larch over the benchmark with one configuration ("experiment").

Each variant is copied to a neutral path `<tmp>/<function>.py` so nothing in the
file name hints at whether it is buggy. Specs are auto-approved (a benchmark
limitation: in real use a person reviews them). LLM responses are cached, so
re-running an experiment, or running experiments that share a stage (same
formalization prompt and model), costs nothing for the shared part. Results are
appended to bench/runs/<name>/results.jsonl, and an interrupted run resumes.

Usage:
  python -m bench.run --name sonnet-hybrid --formalize-mode hybrid --no-proofs --no-mutation
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from larch.config import Config
from larch.engine.session import verify_function
from larch.llm.base import UsageLimitError
from larch.ui import UI

from .domains import DOMAINS

ROOT = Path(__file__).parent
FUNCS = ROOT / "functions"
RUNS = ROOT / "runs"


_quota_lock = threading.Lock()
_resume_at = 0.0


def pause_until_reset(hint: str) -> None:
    """Account usage limit hit: every worker waits until the reset time the CLI
    reported (e.g. "7:40am"), plus a margin, instead of recording bogus failures."""
    global _resume_at
    import datetime as dt
    import re as _re

    now = dt.datetime.now()
    target = now + dt.timedelta(minutes=30)
    m = _re.search(r"(\d{1,2})(?::(\d{2}))?\s*([ap]m)", hint or "", _re.I)
    if m:
        h = int(m.group(1)) % 12 + (12 if m.group(3).lower() == "pm" else 0)
        cand = now.replace(hour=h, minute=int(m.group(2) or 0), second=0, microsecond=0)
        if cand <= now:
            cand += dt.timedelta(days=1)
        target = cand + dt.timedelta(minutes=3)
    with _quota_lock:
        _resume_at = max(_resume_at, target.timestamp())
    print(f"  … usage limit reached ({hint or 'no reset time'}); pausing until {dt.datetime.fromtimestamp(_resume_at):%H:%M}", flush=True)


def wait_for_quota() -> None:
    while True:
        with _quota_lock:
            remaining = _resume_at - time.time()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 60))


def strip_docstrings(src: str) -> str:
    import ast

    tree = ast.parse(src)
    lines = src.splitlines(keepends=True)
    cut: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(getattr(first, "value", None), ast.Constant) and isinstance(first.value.value, str):
                cut.append((first.lineno, first.end_lineno))
    for start, end in sorted(cut, reverse=True):
        del lines[start - 1:end]
    return "".join(lines)


def record_from(report, func: str, variant: str, wall: float) -> dict:
    top = next((f for f in report.findings if f.confidence in ("confirmed", "likely")), None)
    return {
        "function": func,
        "variant": variant,
        "is_bug": variant != "correct",
        "verdict": report.verdict,
        "bug_reported": report.bug_reported,
        "finding_confidence": top.confidence if top else None,
        "finding_kind": top.kind if top else None,
        "finding_example": f"{top.args_repr} -> impl {top.impl} / model {top.model}" if top else None,
        "possible_findings": sum(1 for f in report.findings if f.confidence == "possible"),
        "fix_validated": bool(top and top.fix and top.fix.validated),
        "fix_proposed": bool(top and top.fix),
        "proved": report.proved,
        "total_specs": len(report.active_specs),
        "spec_names": [s.name for s in report.active_specs],
        "proof_methods": {s.name: (s.proof.method if s.proof else "") for s in report.active_specs if s.proof and s.proof.status == "proved"},
        "unproved": {s.name: (s.proof.error[:300] if s.proof else "") for s in report.active_specs if s.proof and s.proof.status == "unproved"},
        "drt_valid": report.drt.get("valid", 0),
        "drt_disagreements": report.drt.get("disagreements", 0),
        "mutation_total": report.mutation.total if report.mutation else 0,
        "mutation_killed": report.mutation.killed if report.mutation else 0,
        "mutation_equiv": report.mutation.likely_equivalent if report.mutation else 0,
        "mutation_by_specs": report.mutation.killed_by_specs if report.mutation else 0,
        "vacuous_specs": [s.name for s in report.specs if s.possibly_vacuous],
        "model_revisions": len(report.model_revisions),
        "cost_usd": report.nominal_cost_usd,
        "spent_usd": report.cost_usd,
        "llm_calls": report.llm_calls,
        "elapsed_s": report.elapsed_s,
        "wall_s": wall,
        "stage_seconds": report.stage_seconds,
        "cost_by_stage": report.cost_by_stage,
        "error": report.error,
        "warnings": report.warnings,
        "artifacts": report.artifacts_dir,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True)
    ap.add_argument("--functions", help="comma-separated subset")
    ap.add_argument("--variants", default="correct,bug1,bug2")
    ap.add_argument("--model", default="claude-sonnet-5")
    ap.add_argument("--prover-model")
    ap.add_argument("--effort", default="high")
    ap.add_argument("--formalize-mode", default="hybrid")
    ap.add_argument("--proof-strategy", default="portfolio+llm")
    ap.add_argument("--proof-attempts", type=int, default=4)
    ap.add_argument("--prover-efforts", default="", help='per-attempt effort schedule, e.g. "medium,medium,high,high"')
    ap.add_argument("--proof-budget", type=float, default=0.30, help="max LLM spend per spec (USD)")
    ap.add_argument("--test-strategy", default="mixed")
    ap.add_argument("--doc-examples", action="store_true", help="extract and check documented examples")
    ap.add_argument("--strip-docstrings", action="store_true", help="remove docstrings (undocumented-code robustness)")
    ap.add_argument("--tests", type=int, default=2000)
    ap.add_argument("--mutants", type=int, default=40)
    ap.add_argument("--no-proofs", action="store_true")
    ap.add_argument("--no-mutation", action="store_true")
    ap.add_argument("--no-fix", action="store_true")
    ap.add_argument("--no-adjudicate", action="store_true")
    ap.add_argument("--parallel", type=int, default=4)
    ap.add_argument("--provider", default="auto")
    ap.add_argument("--salt", default="", help="cache salt (use to draw fresh samples)")
    ap.add_argument("--retry-errors", action="store_true", help="re-run variants that ended in an error")
    args = ap.parse_args(argv)

    out_dir = RUNS / args.name
    out_dir.mkdir(parents=True, exist_ok=True)
    results_path = out_dir / "results.jsonl"
    done: set[tuple[str, str]] = set()
    kept: list[str] = []
    if results_path.exists():
        for line in results_path.read_text().splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            if args.retry_errors and r["verdict"] == "error":
                continue
            done.add((r["function"], r["variant"]))
            kept.append(line)
        results_path.write_text("".join(k + "\n" for k in kept))
    (out_dir / "config.json").write_text(json.dumps(vars(args), indent=2))

    funcs = args.functions.split(",") if args.functions else sorted(DOMAINS)
    variants = args.variants.split(",")
    jobs = [(f, v) for f in funcs for v in variants if (f, v) not in done and (FUNCS / f / f"{v}.py").exists()]
    print(f"[{args.name}] {len(jobs)} runs to do ({len(done)} already done)", flush=True)
    if args.salt:
        import os

        os.environ["LARCH_CACHE_SALT"] = args.salt
    lock = threading.Lock()

    def run_one(func: str, variant: str) -> dict:
        work = out_dir / "src" / func / variant
        if work.exists():
            shutil.rmtree(work)
        work.mkdir(parents=True)
        target = work / f"{func}.py"
        src = (FUNCS / func / f"{variant}.py").read_text()
        target.write_text(strip_docstrings(src) if args.strip_docstrings else src)
        cfg = Config(
            provider=args.provider, model=args.model, prover_model=args.prover_model, effort=args.effort,
            formalize_mode=args.formalize_mode, proof_strategy=args.proof_strategy, proof_attempts=args.proof_attempts,
            test_strategy=args.test_strategy, tests=args.tests, mutants=args.mutants,
            run_proofs=not args.no_proofs, run_mutation=not args.no_mutation, propose_fixes=not args.no_fix,
            adjudicate=not args.no_adjudicate, auto_approve=True, cache=True, budget_usd=8.0,
            prover_efforts=args.prover_efforts, proof_budget_usd=args.proof_budget, doc_examples=args.doc_examples,
            proof_cache=False,  # experiments must not reuse proofs found by other experiments
            artifacts=str(out_dir / "artifacts"),
        )
        cfg.extra["fresh"] = True
        for _attempt in range(4):
            wait_for_quota()
            t0 = time.monotonic()
            try:
                report = verify_function(target, func, cfg, UI())
                break
            except UsageLimitError as e:
                pause_until_reset(e.reset_hint)
        else:
            raise RuntimeError("usage limit persisted across retries")
        rec = record_from(report, func, variant, time.monotonic() - t0)
        with lock:
            with results_path.open("a") as fh:
                fh.write(json.dumps(rec) + "\n")
        return rec

    t_start = time.monotonic()
    with ThreadPoolExecutor(max_workers=args.parallel) as ex:
        futs = {ex.submit(run_one, f, v): (f, v) for f, v in jobs}
        for i, fut in enumerate(as_completed(futs), 1):
            f, v = futs[fut]
            try:
                r = fut.result()
                tag = "BUG" if r["bug_reported"] else ("err" if r["verdict"] == "error" else "ok ")
                expect = "bug" if r["is_bug"] else "ok"
                mark = "✓" if (r["bug_reported"] == r["is_bug"]) and r["verdict"] != "error" else "✗"
                print(f"  {mark} [{i}/{len(jobs)}] {f}/{v}: {tag} (expected {expect}) proved {r['proved']}/{r['total_specs']} "
                      f"${r['cost_usd']:.3f} {r['wall_s']:.0f}s {('ERR ' + r['error'][:80]) if r['error'] else ''}", flush=True)
            except Exception as e:  # noqa: BLE001
                print(f"  ! {f}/{v}: runner exception {e}", flush=True)
    print(f"[{args.name}] finished in {time.monotonic() - t_start:.0f}s", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
