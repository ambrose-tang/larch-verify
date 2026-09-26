"""Validate the benchmark ground truth (no LLM, no Lean).

* every `correct.py` passes its known-answer tests and never crashes in-domain
  (except documented ValueErrors),
* every seeded bug is *observable*: it disagrees with `correct.py` on at least one
  in-domain input; we also report how often (bug "rarity").

Usage: python -m bench.validate [--samples 20000] [--json out.json]
"""
from __future__ import annotations

import argparse
import json
import random
import signal
import sys
from pathlib import Path

from .domains import DOMAINS

ROOT = Path(__file__).parent / "functions"


class _Timeout(BaseException):
    pass


def _alarm(*_):
    raise _Timeout()


def load(func: str, variant: str):
    src = (ROOT / func / f"{variant}.py").read_text()
    ns: dict = {"__name__": f"bench_{func}_{variant}"}
    exec(compile(src, f"{func}/{variant}.py", "exec"), ns)
    return ns[func]


def outcome(fn, args):
    import copy

    signal.setitimer(signal.ITIMER_REAL, 0.5)
    try:
        v = fn(*copy.deepcopy(args))
        return ("ok", type(v).__name__, v)
    except _Timeout:
        return ("timeout",)
    except Exception as e:  # noqa: BLE001
        return ("raise", type(e).__name__)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)


def variants(func: str) -> list[str]:
    return sorted(p.stem for p in (ROOT / func).glob("*.py"))


def validate(samples: int, seed: int = 0) -> dict:
    signal.signal(signal.SIGALRM, _alarm)
    report: dict = {}
    ok = True
    for func in sorted(DOMAINS):
        dom = DOMAINS[func]
        correct = load(func, "correct")
        entry: dict = {"kat_failures": [], "bugs": {}}
        for args, expected in dom["kat"]:
            got = outcome(correct, args)
            want_exc = isinstance(expected, type) and issubclass(expected, Exception)
            if want_exc:
                good = got[0] == "raise" and got[1] == expected.__name__
            else:
                good = got[0] == "ok" and got[2] == expected and type(got[2]) is type(expected)
            if not good:
                entry["kat_failures"].append({"args": repr(args), "expected": repr(expected), "got": repr(got)})
                ok = False
        rng = random.Random(seed)
        inputs = [dom["gen"](rng) for _ in range(samples)]
        ref = [outcome(correct, a) for a in inputs]
        crashes = [a for a, o in zip(inputs, ref) if o[0] == "timeout" or (o[0] == "raise" and o[1] != "ValueError")]
        if crashes:
            entry["correct_crashes"] = repr(crashes[:3])
            ok = False
        for v in variants(func):
            if v == "correct":
                continue
            fn = load(func, v)
            diffs = []
            for a, r in zip(inputs, ref):
                o = outcome(fn, a)
                if o != r:
                    diffs.append((a, r, o))
            kat_caught = any(outcome(fn, a) != outcome(correct, a) for a, _ in dom["kat"])
            doc_visible = any(outcome(fn, a) != outcome(correct, a) for a, _ in dom.get("doc", []))
            entry["bugs"][v] = {
                "observable": bool(diffs),
                "rate": len(diffs) / len(inputs),
                "caught_by_known_answers": kat_caught,
                "visible_in_docstring": doc_visible,
                "example": repr(diffs[0]) if diffs else None,
            }
            if not diffs:
                ok = False
        report[func] = entry
    report["_ok"] = ok
    return report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--samples", type=int, default=20000)
    ap.add_argument("--json")
    args = ap.parse_args(argv)
    rep = validate(args.samples)
    for func, e in rep.items():
        if func.startswith("_"):
            continue
        bugs = "  ".join(
            f"{v}: {'OBSERVABLE' if b['observable'] else 'NOT OBSERVABLE'} ({b['rate']:.1%}{', doc' if b['visible_in_docstring'] else ''})"
            for v, b in e["bugs"].items()
        )
        flag = "" if not e["kat_failures"] and "correct_crashes" not in e else f"  !! {e['kat_failures'] or e.get('correct_crashes')}"
        print(f"{func:18s} {bugs}{flag}")
    print("ALL OK" if rep["_ok"] else "PROBLEMS FOUND")
    if args.json:
        Path(args.json).write_text(json.dumps(rep, indent=2))
    return 0 if rep["_ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
