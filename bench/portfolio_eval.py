"""Measure the no-LLM automation portfolio on saved specs (spec.json files).

Usage: python -m bench.portfolio_eval path/to/spec.json [...]  (or a runs/<exp> directory)
Prints, per spec, which portfolio scripts close it. Costs nothing (Lean only).
"""
from __future__ import annotations

import json
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from larch.config import Config
from larch.engine.prove import Prover, portfolio_scripts
from larch.lean.toolchain import find_toolchain
from larch.lean.workspace import LeanWorkspace
from larch.spec import FormalSpec, model_module


class _Ctx:
    def __init__(self, ws, tc):
        self.ws = ws
        self.tc = tc
        self.cfg = Config(proof_cache=False)


def evaluate(spec_path: Path, tmp: Path) -> dict:
    spec = FormalSpec.from_json(json.loads(spec_path.read_text()))
    tc = find_toolchain()
    ws = LeanWorkspace(tmp / spec_path.parent.name, tc)
    r = ws.compile_module(ws.MODEL, model_module(spec))
    if not r.ok:
        return {"function": spec.function, "error": r.error_text()[:300]}
    prover = Prover(_Ctx(ws, tc), spec)
    out = {}
    for name in spec.spec_names():
        scripts = portfolio_scripts(spec, name)
        with ThreadPoolExecutor(4) as ex:
            res = list(ex.map(lambda lb: (lb[0], prover.try_block(name, lb[1], timeout=60)[0]), scripts))
        out[name] = [label for label, ok in res if ok]
    return {"function": spec.function, "specs": out}


def main(argv=None) -> int:
    args = argv or sys.argv[1:]
    paths: list[Path] = []
    for a in args:
        p = Path(a)
        if p.is_dir():
            paths += sorted(p.rglob("spec.json"))
        else:
            paths.append(p)
    total = solved = 0
    with tempfile.TemporaryDirectory() as td:
        for p in paths:
            r = evaluate(p, Path(td))
            if "error" in r:
                print(f"{r['function']}: model does not compile: {r['error']}")
                continue
            for name, labels in r["specs"].items():
                total += 1
                solved += bool(labels)
                print(f"{r['function']:20s} {name:40s} {'✓ ' + ','.join(labels) if labels else '✗'}")
    print(f"portfolio solved {solved}/{total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
