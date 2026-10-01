"""Terminal UI in the style of Claude Code: streamed steps, spinners, review prompts."""
from __future__ import annotations

import contextlib
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Iterator

from rich.console import Console, Group
from rich.markup import escape
from rich.padding import Padding
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

ACCENT = "#d97757"


@dataclass
class ReviewDecision:
    approved: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    feedback: str = ""  # free-text change request -> re-formalize
    abort: bool = False


class UI:
    """Silent UI (benchmarks, tests). Subclasses render."""

    interactive = False

    def header(self, **kv) -> None: ...

    @contextlib.contextmanager
    def step(self, title: str) -> Iterator["Step"]:
        yield Step(self, title)

    def note(self, text: str, style: str = "") -> None: ...

    def code(self, text: str, lang: str = "lean", title: str = "") -> None: ...

    def show_spec(self, spec, sanity: dict | None = None, auto: bool = False) -> None: ...

    def review(self, spec, sanity: dict | None = None) -> ReviewDecision:
        names = spec.spec_names()
        return ReviewDecision(approved=names)

    def confirm(self, question: str, default: bool = False) -> bool:
        return default

    def final(self, report) -> None: ...


class Step:
    def __init__(self, ui: UI, title: str):
        self.ui = ui
        self.title = title
        self.lines: list[tuple[str, str]] = []
        self.status = "ok"
        self.summary = ""
        self._live_text = ""

    def update(self, text: str) -> None:
        self._live_text = text

    def line(self, text: str, style: str = "") -> None:
        self.lines.append((text, style))

    def done(self, summary: str = "", status: str = "ok") -> None:
        self.summary = summary
        self.status = status


class RichUI(UI):
    def __init__(self, console: Console | None = None, *, verbose: bool = False, interactive: bool | None = None):
        self.c = console or Console(highlight=False)
        self.verbose = verbose
        self.interactive = sys.stdin.isatty() and self.c.is_terminal if interactive is None else interactive
        self._lock = threading.Lock()

    def header(self, **kv) -> None:
        t = Table.grid(padding=(0, 2))
        t.add_column(style="dim")
        t.add_column()
        for k, v in kv.items():
            if v:
                t.add_row(k, str(v))
        title = Text.assemble(("✻ ", ACCENT), ("Larch", "bold"), ("  verification-guided development with Lean 4", "dim"))
        self.c.print(Panel(Group(title, Text(""), t), border_style=ACCENT, expand=False, padding=(0, 2)))
        self.c.print()

    @contextlib.contextmanager
    def step(self, title: str) -> Iterator[Step]:
        st = Step(self, title)
        t0 = time.monotonic()
        if self.c.is_terminal:
            status = self.c.status(Text.assemble((f"{title}…", "bold")), spinner="dots", spinner_style=ACCENT)
            status.start()
            stop = threading.Event()

            def refresh():
                while not stop.wait(0.25):
                    elapsed = time.monotonic() - t0
                    extra = f" · {st._live_text}" if st._live_text else ""
                    status.update(Text.assemble((f"{title}…", "bold"), (f" ({elapsed:.0f}s{extra})", "dim")))

            th = threading.Thread(target=refresh, daemon=True)
            th.start()
        else:
            status = None
            stop = None
        try:
            yield st
        except BaseException:
            st.status = "fail" if st.status == "ok" else st.status
            raise
        finally:
            if status is not None:
                stop.set()
                status.stop()
            elapsed = time.monotonic() - t0
            dot = {"ok": ("●", "green"), "warn": ("●", "yellow"), "fail": ("●", "red"), "info": ("●", ACCENT)}[st.status]
            head = Text.assemble((dot[0] + " ", dot[1]), (st.title, "bold"))
            if st.summary:
                head.append(f"  {st.summary}", style="default")
            head.append(f"  {elapsed:.1f}s" if elapsed >= 0.1 else "", style="dim")
            with self._lock:
                self.c.print(head)
                for i, (text, style) in enumerate(st.lines):
                    prefix = "  ⎿  " if i == 0 else "     "
                    self.c.print(Text(prefix, style="dim") + Text.from_markup(text, style=style or "default"))
                self.c.print()

    def note(self, text: str, style: str = "") -> None:
        self.c.print(Text.from_markup(text, style=style))

    def code(self, text: str, lang: str = "lean", title: str = "") -> None:
        syn = Syntax(text, "lean4" if lang == "lean" else lang, theme="ansi_dark", word_wrap=True, background_color="default")
        self.c.print(Padding(Panel(syn, title=title or None, title_align="left", border_style="dim", expand=True), (0, 0, 0, 5)))

    # -- spec review ---------------------------------------------------------------------
    def _spec_table(self, spec, show_lean: bool) -> Table:
        t = Table(box=None, show_header=False, padding=(0, 1), expand=True)
        t.add_column(width=3, style=ACCENT, no_wrap=True)
        t.add_column(ratio=1)
        idx = 1
        for p in list(spec.active_posts()) + list(spec.active_props()):
            if getattr(p, "origin", "") == "larch":
                continue  # generated by Larch (e.g. the input-domain lemma), not a claim to review
            kind = "postcondition" if p in spec.postconditions else "property of the model"
            if getattr(p, "origin", "") == "contract":
                body = Text.assemble((p.name, "bold"), (f"  {kind} · from LARCH.md\n", "dim"),
                                     ("You wrote:  ", "dim"), (p.contract, ""), ("\nLarch reads: ", "dim"), (p.english, ""))
            else:
                body = Text.assemble((p.name, "bold"), (f"  {kind}\n", "dim"), (p.english, ""))
            if show_lean:
                body.append("\n" + p.lean.strip(), style="dim cyan")
            t.add_row(f"{idx}.", body)
            idx += 1
        return t

    def show_spec(self, spec, sanity: dict | None = None, auto: bool = False) -> None:
        show_lean = self.verbose
        understanding = Text(spec.understanding.strip(), style="")
        pre = Text.assemble(("Assumes: ", "bold"), (spec.pre_english or "nothing (all inputs)", ""))
        parts = [understanding, Text(""), pre, Text(""), self._spec_table(spec, show_lean)]
        if getattr(spec, "examples", None):
            fn = spec.function.split(".")[-1]
            shown = "; ".join(f"{fn}({', '.join(repr(a) for a in e['args'])}) = {e['expected']!r}" for e in spec.examples[:6])
            parts += [Text(""), Text.assemble(("Documented examples checked: ", "bold"), (shown, ""))]
        if sanity and sanity.get("impl_violations"):
            parts += [Text(""), Text("Note: in quick testing the implementation already violates: " + ", ".join(sanity["impl_violations"]), style="yellow")]
        title = "[bold]Specification[/] (auto-approved: --yes)" if auto else "[bold]Proposed specification[/]: please review"
        self.c.print(Panel(Group(*parts), title=title, title_align="left", border_style=ACCENT, padding=(1, 2)))

    def review(self, spec, sanity: dict | None = None) -> ReviewDecision:
        self.show_spec(spec, sanity)
        names = spec.spec_names()
        if not self.interactive:
            self.c.print(Text("  Specs need your approval: rerun in a terminal to review them, or pass --yes to accept them unreviewed.", style="yellow"))
            return ReviewDecision(abort=True)
        return self._prompt_review(spec, names)

    def _prompt_review(self, spec, names) -> ReviewDecision:
        while True:
            self.c.print(Text.assemble(("  [a]", ACCENT), "pprove all   ", ("[r]", ACCENT), "eject some   ", ("[e]", ACCENT), "dit (describe a change)   ", ("[l]", ACCENT), "ean view   ", ("[q]", ACCENT), "uit"))
            choice = self.c.input("  › ").strip().lower() or "a"
            if choice in ("a", "y", "yes"):
                return ReviewDecision(approved=names)
            if choice in ("q", "quit"):
                return ReviewDecision(abort=True)
            if choice in ("l", "lean"):
                self.code(_lean_view(spec), title="Lean statements")
                continue
            if choice in ("r", "reject"):
                raw = self.c.input("  numbers to reject (e.g. 2,4) › ")
                bad = set()
                for tok in raw.replace(" ", ",").split(","):
                    if tok.strip().isdigit():
                        i = int(tok) - 1
                        if 0 <= i < len(names):
                            bad.add(names[i])
                return ReviewDecision(approved=[n for n in names if n not in bad], rejected=sorted(bad))
            if choice in ("e", "edit"):
                fb = self.c.input("  what should change? › ").strip()
                if fb:
                    return ReviewDecision(feedback=fb)

    def confirm(self, question: str, default: bool = False) -> bool:
        if not self.interactive:
            return default
        ans = self.c.input(f"  {question} [{'Y/n' if default else 'y/N'}] › ").strip().lower()
        if not ans:
            return default
        return ans in ("y", "yes")

    # -- final summary -------------------------------------------------------------------
    def final(self, report) -> None:
        colors = {"passed": "green", "bug": "red", "partial": "yellow", "error": "red"}
        labels = {"passed": "PASSED", "bug": "BUG FOUND", "partial": "PARTIALLY VERIFIED", "error": "ERROR"}
        color = colors.get(report.verdict, "white")
        body: list = [Text(report.headline, style="bold")]
        if report.specs:
            t = Table(box=None, show_header=False, padding=(0, 1))
            t.add_column(no_wrap=True)
            t.add_column(style="bold", no_wrap=True)
            t.add_column(style="dim")
            for s in report.specs:
                if s.approval == "rejected":
                    t.add_row(Text("–", style="dim"), s.name, "rejected by reviewer")
                    continue
                p = s.proof
                if p and p.status == "proved":
                    mark, detail = Text("✓ proved", style="green"), p.method
                elif p and p.status == "unproved":
                    mark, detail = Text("✗ unproved", style="yellow"), _short(p.error, 70)
                else:
                    mark, detail = Text("· not attempted", style="dim"), ""
                if s.possibly_vacuous:
                    detail += " · ⚠ may be vacuous"
                if s.impl_violations:
                    detail += f" · violated by implementation on {s.impl_violations} input(s)"
                t.add_row(mark, s.name, detail)
            body += [Text(""), t]
        for f in report.findings:
            body += [
                Text(""),
                Text.assemble(("✗ ", "red"), (f.title(), "bold red"), (f"  ({f.confidence})", "dim")),
                Text.assemble(("   input           ", "dim"), (f"{report.function.split('.')[-1]}({f.args_repr})", "")),
                Text.assemble(("   implementation  ", "dim"), (f.impl, "red")),
                Text.assemble(("   verified model  ", "dim"), (f.model, "green")),
            ]
            if f.violated_specs:
                body.append(Text.assemble(("   violates        ", "dim"), (", ".join(f.violated_specs), "")))
            if f.explanation:
                body.append(Text("   " + f.explanation, style="dim"))
        if report.drt.get("complete"):
            body += [Text(""), Text.assemble(("✓✓ ", "green"), (f"Exhaustive: every one of the {report.drt.get('valid', 0):,} valid inputs was checked "
                                                               f"({report.drt.get('disagreements', 0)} disagreements); the input domain is proved complete", ""))]
        elif report.drt.get("exhaustive"):
            body += [Text(""), Text(f"Exhaustive over the stated ranges: {report.drt.get('valid', 0):,} valid inputs, "
                                    f"{report.drt.get('disagreements', 0)} disagreements (completeness of the ranges is not proved)")]
        elif report.drt:
            body += [Text(""), Text(f"Differential testing: {report.drt.get('valid', 0):,} valid inputs, {report.drt.get('disagreements', 0)} disagreements", style="")]
        if report.mutation and report.mutation.total:
            m = report.mutation
            eq = ""
            if m.likely_equivalent:
                eq = (f"; {m.likely_equivalent} survivor(s) agree with the model (candidate fixes)" if report.bug_reported
                      else f"; {m.likely_equivalent} survivors look equivalent")
            body.append(Text(f"Mutation analysis: {m.killed}/{m.total} injected bugs detected ({m.score:.0%}){eq}; {m.killed_by_specs} caught by the specs alone"))
        for w in report.warnings:
            body.append(Text("⚠ " + w, style="yellow"))
        if report.error:
            body.append(Text(report.error, style="red"))
        self.c.print(Panel(Group(*body), title=f"[bold {color}]{labels.get(report.verdict, report.verdict)}[/]", title_align="left", border_style=color, padding=(1, 2)))
        for f in report.findings:
            if f.fix:
                title = "Proposed fix" + (" · validated against the verified model" if f.fix.validated else " · not validated")
                self.c.print(Padding(Text(title, style="bold"), (0, 0, 0, 1)))
                self.c.print(Padding(Syntax(f.fix.diff, "diff", theme="ansi_dark", background_color="default"), (0, 0, 0, 3)))
                if f.fix.patch_path:
                    self.c.print(Text(f"   saved to {f.fix.patch_path}", style="dim"))
        self.c.print(Text(f"  ${report.cost_usd:.3f} · {report.elapsed_s:.0f}s · {report.llm_calls} LLM calls · artifacts: {report.artifacts_dir}", style="dim"))
        self.c.print()


def _short(s: str, n: int) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _lean_view(spec) -> str:
    lines = [f"-- precondition\n{spec.pre_lean}"]
    for p in spec.active_posts():
        lines.append(f"-- {p.name}: {p.english}\n{p.lean}")
    for q in spec.active_props():
        binders = " ".join(f"({x.name} : {x.lean_type})" for x in q.params)
        lines.append(f"-- {q.name}: {q.english}\n∀ {binders}, {q.lean}")
    lines.append(f"-- model\n{spec.model_code}")
    return "\n\n".join(lines)
