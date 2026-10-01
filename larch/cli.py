"""`larch` command line."""
from __future__ import annotations

import argparse
import json
import re
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
    from .lang import ExtractError, language_for
    from .llm.base import UsageLimitError
    from .ui import RichUI, UI
    from .util import project_root

    overrides = dict(
        provider=args.provider, model=args.model, prover_model=args.prover_model, effort=args.effort,
        tests=args.tests, mutants=args.mutants, budget_usd=args.budget, python=args.python,
        artifacts=args.artifacts, formalize_mode=args.formalize_mode, proof_strategy=args.proof_strategy,
        test_strategy=args.test_strategy, seed=args.seed, proof_attempts=args.proof_attempts,
        prover_efforts=args.prover_efforts, doc_examples=args.doc_examples, node=args.node,
    )
    if args.yes:
        overrides["auto_approve"] = True
    if args.no_mutation:
        overrides["run_mutation"] = False
    if args.no_fix:
        overrides["propose_fixes"] = False
    if args.no_proofs:
        overrides["run_proofs"] = False
    if args.cache:
        overrides["cache"] = True
    reports = []
    if args.approved_only:
        args.reuse = True
    ui = UI() if args.quiet else RichUI(console, verbose=args.verbose)
    plan = _plan_targets(args, console)
    if plan is None:
        return 3
    if not plan and not args.quiet:
        console.print("[yellow]nothing to verify[/] (see `larch scan` for candidates)")
    configs: dict[Path, Config] = {}
    for path, func in plan:
        lang = language_for(path)
        root = project_root(path)
        if root not in configs:
            configs[root] = Config.load(path.resolve().parent, **overrides)
            configs[root].extra["fresh"] = bool(args.fresh)
            configs[root].extra["reuse"] = bool(args.reuse)
        cfg = configs[root]
        try:
            lang.extract(path, func)
        except ExtractError as e:
            console.print(f"[yellow]skipping {path.name}::{func}:[/] {e}")
            continue
        try:
            report = verify_function(path, func, cfg, ui)
        except UsageLimitError as e:
            console.print(f"[red]✗ {e}.[/] Nothing was reported for {func}; re-run after the reset, "
                          "or set ANTHROPIC_API_KEY to use the API instead.")
            return 3
        ui.final(report)
        reports.append(report)
        if report.findings and args.apply:
            _maybe_apply(report, path, console, assume_yes=args.yes)
    if args.json:
        data = [r.to_json() for r in reports]
        Path(args.json).write_text(json.dumps(data if len(data) != 1 else data[0], indent=2, default=str))
    if args.sarif or args.junit or args.markdown:
        from .outputs import write_outputs
        from .util import repo_root

        write_outputs(reports, repo_root(Path.cwd()), sarif=args.sarif, junit=args.junit, markdown=args.markdown)
    if len(reports) > 1 and not args.quiet:
        _summary_table(reports, console)
    if not reports:
        return 0 if ((args.changed is not None or args.approved_only) and plan == []) else 3
    return max(EXIT.get(r.verdict, 3) for r in reports)


def _plan_targets(args, console: Console) -> list[tuple[Path, str]] | None:
    """(file, function) pairs to verify. Directories expand to the functions `larch scan`
    rates as ready; files to all their public functions; --changed keeps only functions
    whose lines differ from the base revision. None means a usage error."""
    from .lang import ExtractError, language_for, supported_extensions
    from .repo import changed_functions, default_base_ref, scan
    from .util import repo_root

    targets = list(args.targets) or (["."] if args.changed is not None else [])
    if not targets:
        console.print("[red]error:[/] give a FILE, FILE::function or DIRECTORY (or --changed)")
        return None
    plan: list[tuple[Path, str]] = []
    for target in targets:
        path, funcs = _parse_target(target)
        if not path.exists():
            console.print(f"[red]error:[/] {path} does not exist")
            return None
        if path.is_dir():
            cands = [c for c in scan([path], include_tests=args.include_tests) if c.status == "ready"]
            base = repo_root(path)
            plan += [((base / c.file).resolve(), c.function) for c in cands]
            continue
        lang = language_for(path)
        if lang is None:
            console.print(f"[red]error:[/] {path}: unsupported file type (supported: {', '.join(supported_extensions())})")
            return None
        if not funcs:
            try:
                funcs = lang.list_functions(path)
            except (SyntaxError, ExtractError) as e:
                console.print(f"[red]error:[/] cannot parse {path}: {e}")
                return None
            if not funcs:
                console.print(f"[yellow]no public functions found in {path}[/]")
        plan += [(path.resolve(), f) for f in funcs]
    if args.k:
        plan = [(p, f) for p, f in plan if args.k in f]
    if args.changed is not None:
        root = repo_root(Path(targets[0].split("::")[0]))
        ref = args.changed or default_base_ref(root)
        try:
            files = sorted({p for p, _ in plan})
            cands = scan(files, include_tests=True, root=root)
            touched = {((root / c.file).resolve(), c.function) for c in changed_functions(cands, root, ref)}
        except ValueError as e:
            console.print(f"[red]error:[/] {e}")
            return None
        plan = [(p, f) for p, f in plan if (p, f) in touched]
        if not args.quiet:
            console.print(f"[dim]{len(plan)} function(s) changed since {ref[:12]}[/]")
    seen: set = set()
    plan = [x for x in plan if not (x in seen or seen.add(x))]
    if args.approved_only:
        from .engine.store import load_approved

        kept = []
        for p, f in plan:
            try:
                if load_approved(language_for(p).extract(p, f)) is not None:
                    kept.append((p, f))
            except ExtractError:
                continue
        if not args.quiet and len(kept) < len(plan):
            console.print(f"[dim]{len(plan) - len(kept)} function(s) without approved specs skipped (--approved-only)[/]")
        plan = kept
    if args.limit:
        plan = plan[: args.limit]
    return plan


def _maybe_apply(report, path: Path, console: Console, assume_yes: bool) -> None:
    from .util import sha256

    fix = next((f.fix for f in report.findings if f.fix and f.fix.validated), None)
    if fix is None or not fix.new_source:
        console.print("[yellow]No validated fix to apply.[/]")
        return
    current = path.read_text()
    if fix.base_sha256 and sha256(current) != fix.base_sha256:
        console.print(f"[yellow]{path} changed since it was verified; not applying. Re-run larch.[/]")
        return
    if not assume_yes:
        if not sys.stdin.isatty():
            console.print("[yellow]Not applying the fix without confirmation (non-interactive). Use --apply --yes.[/]")
            return
        ans = console.input(f"  Apply the validated fix to {path}? [y/N] › ").strip().lower()
        if ans not in ("y", "yes"):
            return
    tmp = path.with_suffix(path.suffix + ".larch-tmp")
    tmp.write_text(fix.new_source)
    os.replace(tmp, path)
    fix.applied = True
    console.print(f"[green]Applied the validated fix to {path}.[/]")


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
    from .py.env import PythonEnvError, resolve_python

    try:
        cfg = Config.load(Path.cwd())
        env = resolve_python(Path.cwd() / "_.py", cfg.python)
        mark = "[yellow]![/]" if env.source == "Larch's own interpreter" else "[green]✓[/]"
        console.print(f"{mark} Python for your code here: {env.describe()}")
        for w in env.warnings[:1]:
            console.print(f"    [dim]{w}[/]")
    except PythonEnvError as e:
        ok = False
        console.print(f"[red]✗[/] Python: {e}")
    import shutil

    node = shutil.which("node")
    console.print(f"[green]✓[/] Node.js for JavaScript/TypeScript: {node}" if node else
                  "[dim]·[/] Node.js not found (needed only for JavaScript/TypeScript)")
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


def cmd_scan(args, console: Console) -> int:
    from rich.table import Table

    from .repo import changed_functions, default_base_ref, scan
    from .util import repo_root

    paths = [Path(p) for p in (args.paths or ["."])]
    for p in paths:
        if not p.exists():
            console.print(f"[red]error:[/] {p} does not exist")
            return 3
    root = repo_root(paths[0])
    cands = scan(paths, include_tests=args.include_tests, root=root)
    if args.changed is not None:
        try:
            cands = changed_functions(cands, root, args.changed or default_base_ref(root))
        except ValueError as e:
            console.print(f"[red]error:[/] {e}")
            return 3
    ready = [c for c in cands if c.status == "ready"]
    skipped = [c for c in cands if c.status != "ready"]
    if args.json:
        Path(args.json).write_text(json.dumps({"root": str(root), "candidates": [c.to_json() for c in cands]}, indent=2))
    by_lang: dict[str, int] = {}
    for c in ready:
        by_lang[c.language] = by_lang.get(c.language, 0) + 1
    langs = ", ".join(f"{n} {lang}" for lang, n in sorted(by_lang.items()))
    console.print(f"[bold]{len(ready)} verifiable function(s)[/]" + (f" ({langs})" if langs else "")
                  + f", {len(skipped)} skipped, under {root}")
    shown = ready if args.all else ready[: args.top]
    if shown:
        t = Table(box=None, padding=(0, 2), show_edge=False)
        for col in ("score", "target", "lines", "docs"):
            t.add_column(col, style="dim" if col in ("score", "lines") else None, overflow="fold")
        for c in shown:
            t.add_row(f"{c.score:.1f}", c.target, str(c.end_line - c.line + 1), "✓" if c.documented else "")
        console.print(t)
        if len(ready) > len(shown):
            console.print(f"[dim]… {len(ready) - len(shown)} more (use --all or --json)[/]")
    if skipped:
        reasons: dict[str, int] = {}
        for c in skipped:
            key = re.sub(r"\s+", " ", re.sub(r"`[^`]*`|\([^)]*\)|\d+ lines", "", c.reason)).strip(" :") or c.reason
            reasons[key] = reasons.get(key, 0) + 1
        console.print("[dim]skipped: " + "; ".join(f"{n}× {r}" for r, n in sorted(reasons.items(), key=lambda kv: -kv[1])[:6]) + "[/]")
        if args.all:
            for c in skipped:
                console.print(f"[dim]  · {c.target}: {c.reason}[/]")
    if ready:
        console.print(f"\nNext: [bold]larch verify {shown[0].target}[/]  or  [bold]larch verify {paths[0]} --limit 10[/]")
    return 0


def cmd_list(args, console: Console) -> int:
    from .lang import ExtractError, language_for

    lang = language_for(args.file)
    if lang is None:
        console.print(f"[red]error:[/] {args.file}: unsupported file type")
        return 3
    try:
        for f in lang.list_functions(Path(args.file)):
            console.print(f"{args.file}::{f}")
    except (SyntaxError, ExtractError) as e:
        console.print(f"[red]error:[/] {e}")
        return 3
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="larch",
        description="Verification-guided development: Lean 4 models, proofs, and differential tests for your functions.",
    )
    p.add_argument("--version", action="version", version=f"larch {__version__}")
    sub = p.add_subparsers(dest="cmd")

    v = sub.add_parser("verify", help="verify functions (FILE.py::func, or FILE.py for all public functions)")
    v.add_argument("targets", nargs="*", help="FILE, FILE::function, or DIRECTORY (default with --changed: the repository)")
    v.add_argument("-k", help="only functions whose name contains this substring")
    v.add_argument("--changed", nargs="?", const="", metavar="REF",
                   help="only functions changed since REF (default: merge base with the default branch)")
    v.add_argument("--limit", type=int, help="verify at most N functions")
    v.add_argument("--include-tests", action="store_true", help="also consider functions in test files (directories)")
    v.add_argument("-y", "--yes", action="store_true", help="accept proposed specs without review (CI/benchmarks)")
    v.add_argument("--reuse", action="store_true", help="reuse previously approved specs without asking")
    v.add_argument("--fresh", action="store_true", help="ignore previously approved specs")
    v.add_argument("--approved-only", action="store_true",
                   help="only functions with specs a person approved (in .larch/specs); implies --reuse (CI)")
    v.add_argument("--apply", action="store_true", help="apply a validated fix to your file (asks first)")
    v.add_argument("--model", help="LLM for formalization, adjudication and fixes (default claude-sonnet-5)")
    v.add_argument("--prover-model", help="LLM for proofs (default: same as --model)")
    v.add_argument("--provider", choices=["auto", "anthropic", "bedrock", "vertex", "claude-code"])
    v.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"])
    v.add_argument("--tests", type=int, help="random inputs for differential testing (default 2000)")
    v.add_argument("--mutants", type=int, help="max implementation mutants (default 40)")
    v.add_argument("--no-mutation", action="store_true")
    v.add_argument("--no-fix", action="store_true", help="do not propose fixes")
    v.add_argument("--no-proofs", action="store_true",
                   help="skip proving (re-test approved specs only; the verdict is then at best `partial`)")
    v.add_argument("--budget", type=float, help="max LLM spend in USD per function (default 5)")
    v.add_argument("--python", help="Python interpreter or virtualenv for your code (default: discovered: project venv, "
                   "$VIRTUAL_ENV, conda, poetry/pipenv/pdm/hatch, PATH)")
    v.add_argument("--node", help="Node.js binary for JavaScript/TypeScript (default: `node` on PATH)")
    v.add_argument("--artifacts", help="directory for run artifacts (default ~/.cache/larch/runs)")
    v.add_argument("--formalize-mode", choices=["auto", "hybrid", "intent", "transliterate"])
    v.add_argument("--proof-strategy", choices=["portfolio", "llm", "portfolio+llm", "portfolio+sketch"])
    v.add_argument("--test-strategy", choices=["typed", "llm", "mixed"])
    v.add_argument("--doc-examples", action=argparse.BooleanOptionalAction, default=None,
                   help="check examples written in the docstring against the model and the code (default on)")
    v.add_argument("--proof-attempts", type=int)
    v.add_argument("--prover-efforts", help='per-attempt reasoning effort, e.g. "medium,medium,high,high"')
    v.add_argument("--seed", type=int)
    v.add_argument("--cache", action="store_true", help="cache LLM responses on disk")
    v.add_argument("--json", help="write the report(s) as JSON to this path")
    v.add_argument("--sarif", help="write findings as SARIF 2.1.0 (code scanning) to this path")
    v.add_argument("--junit", help="write a JUnit XML report to this path")
    v.add_argument("--markdown", help="write a Markdown summary (PR comment / $GITHUB_STEP_SUMMARY); '-' for stdout")
    v.add_argument("-v", "--verbose", action="store_true", help="show Lean statements during review")
    v.add_argument("-q", "--quiet", action="store_true", help="no terminal UI (use with --json)")

    d = sub.add_parser("doctor", help="check the Lean toolchain, proof checker and LLM provider")
    d.add_argument("--install", action="store_true", help="install the pinned Lean toolchain with elan if missing")
    d.add_argument("--provider", choices=["auto", "anthropic", "bedrock", "vertex", "claude-code"])

    i = sub.add_parser("init", help="store approved specs in this project (.larch/specs) so they can be committed")
    i.add_argument("dir", nargs="?", default=".")

    s = sub.add_parser("show", help="show the report of a previous run (default: latest)")
    s.add_argument("run", nargs="?")

    sc = sub.add_parser("scan", help="find and rank the functions in a repository that Larch can verify")
    sc.add_argument("paths", nargs="*", help="directories or files (default: .)")
    sc.add_argument("--top", type=int, default=25, help="show the N best candidates (default 25)")
    sc.add_argument("--all", action="store_true", help="show every candidate and every skipped function with its reason")
    sc.add_argument("--changed", nargs="?", const="", metavar="REF", help="only functions changed since REF")
    sc.add_argument("--include-tests", action="store_true", help="include test files")
    sc.add_argument("--json", help="write all candidates (with skip reasons) as JSON")

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
            "  [bold]larch scan[/] [repo]                         find the functions worth verifying in a repository\n"
            "  [bold]larch verify --changed[/]                     verify the functions a branch changed (CI)\n"
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
        if args.cmd == "scan":
            return cmd_scan(args, console)
    except KeyboardInterrupt:
        console.print("\n[dim]interrupted[/]")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
