"""`larch` command line."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from rich.console import Console

from . import __version__
from .config import Config

EXIT = {"passed": 0, "bug": 1, "partial": 2, "error": 3}


def _parse_target(t: str) -> tuple[Path, list[str]]:
    if "::" in t:
        f, func = t.split("::", 1)
        return Path(f), [func]
    return Path(t), []


def cmd_verify(args, console: Console) -> int:
    from .engine.session import verify_function
    from .py.extract import ExtractError, extract, list_functions
    from .ui import RichUI, UI

    overrides = dict(
        provider=args.provider, model=args.model, prover_model=args.prover_model, effort=args.effort,
        tests=args.tests, mutants=args.mutants, budget_usd=args.budget, python=args.python,
        artifacts=args.artifacts, formalize_mode=args.formalize_mode, proof_strategy=args.proof_strategy,
        test_strategy=args.test_strategy, seed=args.seed, proof_attempts=args.proof_attempts,
        prover_efforts=args.prover_efforts,
    )
    if args.yes:
        overrides["auto_approve"] = True
    if args.no_mutation:
        overrides["run_mutation"] = False
    if args.no_fix:
        overrides["propose_fixes"] = False
    if args.cache:
        overrides["cache"] = True
    reports = []
    ui = UI() if args.quiet else RichUI(console, verbose=args.verbose)
    for target in args.targets:
        path, funcs = _parse_target(target)
        if not path.exists():
            console.print(f"[red]error:[/] {path} does not exist")
            return 3
        if path.suffix != ".py":
            console.print(f"[red]error:[/] {path}: only Python files are supported in this version")
            return 3
        if not funcs:
            try:
                funcs = list_functions(path)
            except SyntaxError as e:
                console.print(f"[red]error:[/] cannot parse {path}: {e}")
                return 3
            if args.k:
                funcs = [f for f in funcs if args.k in f]
            if not funcs:
                console.print(f"[yellow]no public functions found in {path}[/]")
                continue
        cfg = Config.load(path.resolve().parent, **overrides)
        cfg.extra["fresh"] = bool(args.fresh)
        cfg.extra["reuse"] = bool(args.reuse)
        for func in funcs:
            try:
                extract(path, func)
            except ExtractError as e:
                console.print(f"[yellow]skipping {path.name}::{func}:[/] {e}")
                continue
            report = verify_function(path, func, cfg, ui)
            ui.final(report)
            reports.append(report)
            if report.findings and args.apply:
                _maybe_apply(report, path, console, assume_yes=args.yes)
    if args.json:
        data = [r.to_json() for r in reports]
        Path(args.json).write_text(json.dumps(data if len(data) != 1 else data[0], indent=2, default=str))
    if len(reports) > 1 and not args.quiet:
        _summary_table(reports, console)
    if not reports:
        return 3
    return max(EXIT.get(r.verdict, 3) for r in reports)


def _maybe_apply(report, path: Path, console: Console, assume_yes: bool) -> None:
    fix = next((f.fix for f in report.findings if f.fix and f.fix.validated), None)
    if fix is None:
        console.print("[yellow]No validated fix to apply.[/]")
        return
    if not assume_yes:
        if not sys.stdin.isatty():
            console.print("[yellow]Not applying the fix without confirmation (non-interactive). Use --apply --yes.[/]")
            return
        ans = console.input(f"  Apply the validated fix to {path}? [y/N] › ").strip().lower()
        if ans not in ("y", "yes"):
            return
    import subprocess

    r = subprocess.run(["patch", "-p1", "--forward", str(path)], input=fix.diff, text=True, capture_output=True)
    if r.returncode == 0:
        fix.applied = True
        console.print(f"[green]Applied fix to {path}.[/]")
    else:
        console.print(f"[red]Could not apply the patch:[/] {r.stdout or r.stderr}")


def _summary_table(reports, console: Console) -> None:
    from rich.table import Table

    t = Table(title="Summary", show_lines=False)
    for col in ("function", "verdict", "proved", "tests", "mutation", "cost", "time"):
        t.add_column(col)
    for r in reports:
        m = r.mutation
        t.add_row(
            r.function,
            {"passed": "[green]passed[/]", "bug": "[red]bug[/]", "partial": "[yellow]partial[/]"}.get(r.verdict, "[red]error[/]"),
            f"{r.proved}/{len(r.active_specs)}",
            f"{r.drt.get('valid', 0):,}",
            f"{m.score:.0%}" if m and m.total else "-",
            f"${r.cost_usd:.2f}",
            f"{r.elapsed_s:.0f}s",
        )
    console.print(t)


def cmd_doctor(args, console: Console) -> int:
    from .lean.checker import Checker
    from .lean.toolchain import PINNED_TOOLCHAIN, ToolchainError, find_toolchain
    from .llm.providers import make_provider

    ok = True
    console.print(f"[bold]larch {__version__}[/]  (pinned Lean toolchain {PINNED_TOOLCHAIN})")
    try:
        tc = find_toolchain(install=args.install)
        console.print(f"[green]✓[/] Lean toolchain: {tc.prefix}")
        with console.status("building the proof checker (one-time, ~20s)…"):
            exe = Checker(tc).binary(build=True)
        if exe:
            console.print(f"[green]✓[/] proof checker: {exe}")
        else:
            console.print("[yellow]![/] could not build the native proof checker; falling back to the (slower) interpreted checker")
    except ToolchainError as e:
        ok = False
        console.print(f"[red]✗[/] {e}")
    try:
        p = make_provider(args.provider or "auto")
        console.print(f"[green]✓[/] LLM provider: {p.describe()}")
    except Exception as e:  # noqa: BLE001
        ok = False
        console.print(f"[red]✗[/] {e}")
    console.print(f"[green]✓[/] Python for user code: {sys.executable} (override with --python or LARCH_PYTHON)")
    return 0 if ok else 3


def cmd_init(args, console: Console) -> int:
    root = Path(args.dir).resolve()
    d = root / ".larch"
    d.mkdir(exist_ok=True)
    (d / "specs").mkdir(exist_ok=True)
    cfg = root / ".larch.toml"
    if not cfg.exists():
        cfg.write_text(
            "# Larch project configuration (see `larch verify --help`)\n"
            "# model = \"claude-sonnet-5\"\n# tests = 2000\n# mutants = 40\n# budget_usd = 5.0\n"
        )
    console.print(f"[green]Initialized[/] {d}. Approved specs will be stored in {d / 'specs'} so they can be reviewed and committed.")
    return 0


def cmd_show(args, console: Console) -> int:
    from .engine.session import artifacts_root
    from rich.markdown import Markdown

    cfg = Config.load(Path.cwd())
    if args.run in (None, "latest"):
        runs = sorted((p for p in artifacts_root(cfg).glob("*") if (p / "report.md").exists()), key=lambda p: p.stat().st_mtime)
        if not runs:
            console.print("no runs yet")
            return 3
        run = runs[-1]
    else:
        run = Path(args.run)
    console.print(Markdown((run / "report.md").read_text()))
    console.print(f"[dim]{run}[/]")
    return 0


def cmd_list(args, console: Console) -> int:
    from .py.extract import list_functions

    for f in list_functions(Path(args.file)):
        console.print(f"{args.file}::{f}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="larch",
        description="Verification-guided development: Lean 4 models, proofs, and differential tests for your functions.",
    )
    p.add_argument("--version", action="version", version=f"larch {__version__}")
    sub = p.add_subparsers(dest="cmd")

    v = sub.add_parser("verify", help="verify functions (FILE.py::func, or FILE.py for all public functions)")
    v.add_argument("targets", nargs="+")
    v.add_argument("-k", help="only functions whose name contains this substring")
    v.add_argument("-y", "--yes", action="store_true", help="accept proposed specs without review (CI/benchmarks)")
    v.add_argument("--reuse", action="store_true", help="reuse previously approved specs without asking")
    v.add_argument("--fresh", action="store_true", help="ignore previously approved specs")
    v.add_argument("--apply", action="store_true", help="apply a validated fix to your file (asks first)")
    v.add_argument("--model", help="LLM for formalization/adjudication/fixes (default claude-sonnet-5)")
    v.add_argument("--prover-model", help="LLM for proofs (default: same as --model)")
    v.add_argument("--provider", choices=["auto", "anthropic", "claude-code"])
    v.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"])
    v.add_argument("--tests", type=int, help="random inputs for differential testing (default 2000)")
    v.add_argument("--mutants", type=int, help="max implementation mutants (default 40)")
    v.add_argument("--no-mutation", action="store_true")
    v.add_argument("--no-fix", action="store_true")
    v.add_argument("--budget", type=float, help="max LLM spend in USD per function (default 5)")
    v.add_argument("--python", help="interpreter used to run your code (default: project .venv or current)")
    v.add_argument("--artifacts", help="directory for run artifacts (default ~/.cache/larch/runs)")
    v.add_argument("--formalize-mode", choices=["hybrid", "intent", "transliterate"])
    v.add_argument("--proof-strategy", choices=["portfolio", "llm", "portfolio+llm", "portfolio+sketch"])
    v.add_argument("--test-strategy", choices=["typed", "llm", "mixed"])
    v.add_argument("--proof-attempts", type=int)
    v.add_argument("--prover-efforts", help='per-attempt reasoning effort, e.g. "medium,medium,high,high"')
    v.add_argument("--seed", type=int)
    v.add_argument("--cache", action="store_true", help="cache LLM responses on disk")
    v.add_argument("--json", help="write the report(s) as JSON to this path")
    v.add_argument("-v", "--verbose", action="store_true", help="show Lean statements during review")
    v.add_argument("-q", "--quiet", action="store_true", help="no terminal UI (use with --json)")

    d = sub.add_parser("doctor", help="check the Lean toolchain, proof checker and LLM provider")
    d.add_argument("--install", action="store_true", help="install the pinned Lean toolchain with elan if missing")
    d.add_argument("--provider", choices=["auto", "anthropic", "claude-code"])

    i = sub.add_parser("init", help="store approved specs in this project (.larch/specs) so they can be committed")
    i.add_argument("dir", nargs="?", default=".")

    s = sub.add_parser("show", help="show the report of a previous run (default: latest)")
    s.add_argument("run", nargs="?")

    ls = sub.add_parser("list", help="list the functions Larch can verify in a file")
    ls.add_argument("file")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    console = Console(highlight=False)
    if args.cmd is None:
        console.print(
            "[bold #d97757]✻ Larch[/] [dim]verification-guided development with Lean 4[/]\n\n"
            "  [bold]larch verify[/] path/to/file.py::function   verify one function\n"
            "  [bold]larch verify[/] path/to/file.py             verify every public function in a file\n"
            "  [bold]larch doctor --install[/]                   set up Lean and check your LLM access\n"
            "  [bold]larch show[/]                               show the last report\n\n"
            "[dim]Run `larch verify --help` for options.[/]"
        )
        return 0
    try:
        if args.cmd == "verify":
            return cmd_verify(args, console)
        if args.cmd == "doctor":
            return cmd_doctor(args, console)
        if args.cmd == "init":
            return cmd_init(args, console)
        if args.cmd == "show":
            return cmd_show(args, console)
        if args.cmd == "list":
            return cmd_list(args, console)
    except KeyboardInterrupt:
        console.print("\n[dim]interrupted[/]")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
