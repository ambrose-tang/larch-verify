"""Summarize benchmark experiments as Markdown tables (for EVALS.md).

Usage:
  python -m bench.analyze EXP1 EXP2 ...            # headline table
  python -m bench.analyze EXP --detail             # per-function breakdown
  python -m bench.analyze EXP1 EXP2 --proofs       # proof-centric table (correct variants)
"""
from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path

ROOT = Path(__file__).parent
RUNS = ROOT / "runs"


def load(name: str) -> list[dict]:
    p = RUNS / name / "results.jsonl"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


def rarity() -> dict[tuple[str, str], float]:
    gt = ROOT / "ground_truth.json"
    if not gt.exists():
        return {}
    d = json.loads(gt.read_text())
    out = {}
    for f, e in d.items():
        if f.startswith("_"):
            continue
        for v, b in e["bugs"].items():
            out[(f, v)] = b["rate"]
    return out


def pct(a: int, b: int) -> str:
    return f"{a}/{b} ({a / b:.0%})" if b else "-"


def summarize(name: str) -> dict:
    rs = load(name)
    rare = rarity()
    bugs = [r for r in rs if r["is_bug"]]
    oks = [r for r in rs if not r["is_bug"]]
    caught = [r for r in bugs if r["bug_reported"]]
    confirmed = [r for r in bugs if r["bug_reported"] and r["finding_confidence"] == "confirmed"]
    false_alarms = [r for r in oks if r["bug_reported"]]
    rare_bugs = [r for r in bugs if rare.get((r["function"], r["variant"]), 1.0) < 0.05]
    rare_caught = [r for r in rare_bugs if r["bug_reported"]]
    errors = [r for r in rs if r["verdict"] == "error"]
    proved = sum(r["proved"] for r in oks)
    total_specs = sum(r["total_specs"] for r in oks)
    fully = [r for r in oks if r["total_specs"] and r["proved"] == r["total_specs"]]
    attempted = [r for r in oks if r["proof_methods"] or r["unproved"]]
    costs = [r["cost_usd"] for r in rs]
    walls = [r["wall_s"] for r in rs]
    fixes = [r for r in caught if r.get("fix_proposed")]
    fixes_ok = [r for r in caught if r.get("fix_validated")]
    mut_total = sum(r["mutation_total"] for r in oks)
    mut_killed = sum(r["mutation_killed"] for r in oks)
    mut_equiv = sum(r["mutation_equiv"] for r in oks)
    mut_specs = sum(r["mutation_by_specs"] for r in oks)
    prove_cost = [r["cost_by_stage"].get("prove", {}).get("cost_usd", 0) + r["cost_by_stage"].get("prove-sketch", {}).get("cost_usd", 0) + r["cost_by_stage"].get("prove-lemma", {}).get("cost_usd", 0) for r in oks]
    prove_secs = [r["stage_seconds"].get("prove", 0) for r in oks]
    return {
        "name": name,
        "runs": len(rs),
        "errors": len(errors),
        "caught": (len(caught), len(bugs)),
        "confirmed": (len(confirmed), len(bugs)),
        "rare": (len(rare_caught), len(rare_bugs)),
        "false_alarms": (len(false_alarms), len(oks)),
        "possible": sum(1 for r in rs if r["possible_findings"]),
        "proved": (proved, total_specs),
        "fully": (len(fully), len(oks)),
        "attempted": len(attempted),
        "cost_mean": statistics.mean(costs) if costs else 0,
        "cost_total": sum(costs),
        "wall_mean": statistics.mean(walls) if walls else 0,
        "wall_median": statistics.median(walls) if walls else 0,
        "fixes": (len(fixes_ok), len(caught), len(fixes)),
        "mutation": (mut_killed, mut_total, mut_equiv, mut_specs),
        "prove_cost_mean": statistics.mean(prove_cost) if prove_cost else 0,
        "prove_secs_mean": statistics.mean(prove_secs) if prove_secs else 0,
        "records": rs,
    }


def headline(names: list[str]) -> str:
    rows = ["| experiment | bugs caught | rare bugs (<5% of inputs) | false alarms | errors | cost / fn | time / fn |",
            "|---|---|---|---|---|---|---|"]
    for n in names:
        s = summarize(n)
        if not s["runs"]:
            continue
        rows.append(
            f"| {n} | {pct(*s['caught'])} | {pct(*s['rare'])} | {pct(*s['false_alarms'])} | {s['errors']} | "
            f"${s['cost_mean']:.3f} | {s['wall_mean']:.0f}s |"
        )
    return "\n".join(rows)


def proofs_table(names: list[str]) -> str:
    rows = ["| experiment | specs proved | functions fully proved | proof cost / fn | proof time / fn |",
            "|---|---|---|---|---|"]
    for n in names:
        s = summarize(n)
        if not s["runs"]:
            continue
        rows.append(
            f"| {n} | {pct(*s['proved'])} | {pct(*s['fully'])} | ${s['prove_cost_mean']:.3f} | {s['prove_secs_mean']:.0f}s |"
        )
    return "\n".join(rows)


def detail(name: str) -> str:
    s = summarize(name)
    rare = rarity()
    by_f: dict[str, dict[str, dict]] = {}
    for r in s["records"]:
        by_f.setdefault(r["function"], {})[r["variant"]] = r
    rows = ["| function | correct | bug1 | bug2 | proved (correct) |", "|---|---|---|---|---|"]

    def cell(r, is_bug):
        if r is None:
            return "–"
        if r["verdict"] == "error":
            return "ERROR"
        if is_bug:
            return ("caught" + (" (conf.)" if r["finding_confidence"] == "confirmed" else " (likely)")) if r["bug_reported"] else "**missed**"
        return "**false alarm**" if r["bug_reported"] else "clean"

    for f in sorted(by_f):
        v = by_f[f]
        c = v.get("correct")
        rows.append(
            f"| {f} | {cell(c, False)} | {cell(v.get('bug1'), True)} ({rare.get((f, 'bug1'), 0):.0%}) | "
            f"{cell(v.get('bug2'), True)} ({rare.get((f, 'bug2'), 0):.0%}) | "
            f"{(str(c['proved']) + '/' + str(c['total_specs'])) if c else '–'} |"
        )
    return "\n".join(rows)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("names", nargs="+")
    ap.add_argument("--detail", action="store_true")
    ap.add_argument("--proofs", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    if args.json:
        print(json.dumps({n: {k: v for k, v in summarize(n).items() if k != "records"} for n in args.names}, indent=2))
        return 0
    print(headline(args.names))
    if args.proofs:
        print()
        print(proofs_table(args.names))
    if args.detail:
        for n in args.names:
            print(f"\n### {n}\n")
            print(detail(n))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
