"""`larch`: an interactive session that drafts LARCH.md with you, then verifies it.

Phase 1 is a short conversation. Larch looks at the repository (a compact summary, plus
the source of anything it asks to read), asks what the code is *for*, and turns your
answers into contracts, which it confirms with you. Every change to LARCH.md is kept in
`.larch/history/`, so `/undo` and `/rollback` can take any of them back.

Phase 2 runs `larch verify` on the result. If anything fails you get a summary to work
from; if everything passes, a Larch certificate that pins what was verified (the
contracts, the source files, the proofs) by hash.

The session logic (`ChatSession`) is separate from the terminal UI (`run`), so it can be
driven and tested without a terminal."""
from __future__ import annotations

import difflib
import hashlib
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from rich import box
from rich.align import Align
from rich.console import Console, Group
from rich.markdown import Markdown
from rich.padding import Padding
from rich.panel import Panel
from rich.rule import Rule
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from . import __version__
from . import contracts as larchmd
from .config import Config
from .llm.base import LLM, Ledger, LLMError, LLMRequest

# A larch is a conifer whose needles turn gold in autumn: gold and bark, not a harness's palette.
GOLD, BARK, NEEDLE, MUTED = "#e3b341", "#a0785a", "#7fb069", "#8a8f98"
MARK = "▲"

CHAT_SCHEMA = {
    "type": "object",
    "properties": {
        "say": {"type": "string", "description": "your message to the developer: short, plain, markdown allowed"},
        "choices": {"type": "array", "items": {"type": "string"},
                    "description": "2-5 short answers to pick from when the question has natural options; else []"},
        "read": {"type": "array", "items": {"type": "string"},
                 "description": "path::name of code you must read before answering (max 3); `say` may then be empty"},
        "draft": {"type": "string", "description": "the COMPLETE new LARCH.md if it changes this turn; else \"\""},
        "change": {"type": "string", "description": "one line: what changed in the draft (if it changed)"},
        "ready": {"type": "boolean", "description": "true when the draft captures what the developer confirmed"},
    },
    "required": ["say", "choices", "read", "draft", "change", "ready"],
    "additionalProperties": False,
}

SYSTEM = """\
You are Larch, helping a developer write LARCH.md: the plain-English contracts their code
must meet. Larch later turns each contract into Lean, proves it about a model of the
code, and tests the real code against that model.

How to talk:
- Ask about INTENT, not specs: what is this code for, what must never happen, what would
  be a costly bug, what do callers rely on. Then propose contracts in your own words and
  confirm them ("So: a transfer never changes the total credit. Right?"). Do not make the
  developer write contracts; do not interrogate. Be natural and brief: 1-3 sentences
  and at most one question per turn.
- Offer `choices` when the answer has natural options (yes / adjust / skip; which of
  these modules matters most). Leave it empty for open questions.
- Read code only when you need it (`read`), a few items at a time.
- Update `draft` as soon as something is agreed, and say what changed. Never invent a
  contract the developer has not agreed to; leave a heading without bullets to let Larch
  propose contracts later.
- Set `ready` once the important behaviour is covered and confirmed; ask whether to verify.

LARCH.md format:
```
# Contracts

## path/to/file.py::function_or_Class      (paths relative to the repository root)
- One contract per bullet, in plain English.

## service NAME                            (an HTTP service Larch starts itself)
start: uvicorn app.main:app --port {port}
database: postgres                          (postgres | sqlite | none)
- A contract about the endpoints.

# System rules
- A guarantee about components together.  (uses: path/to/file.py::Class, service NAME)
```
Only use subjects that exist in the repository summary or that the developer named.
"""


@dataclass
class Turn:
    who: str  # "you" | "larch" | "code"
    text: str


@dataclass
class Version:
    n: int
    note: str
    path: Path | None  # snapshot file; None = LARCH.md did not exist
    at: str


@dataclass
class ChatSession:
    root: Path
    cfg: Config
    llm: LLM
    md: Path = field(init=False)
    turns: list[Turn] = field(default_factory=list)
    draft: str = ""
    ready: bool = False
    versions: list[Version] = field(default_factory=list)
    summary: str = ""

    def __post_init__(self) -> None:
        self.md = self.root / larchmd.FILENAME
        self.hist = self.root / ".larch" / "history"
        self.hist.mkdir(parents=True, exist_ok=True)
        self.draft = self.md.read_text() if self.md.exists() else ""
        self._snapshot("start of session" if self.draft else "no LARCH.md yet")

    # -- versions ----------------------------------------------------------------------------
    def _snapshot(self, note: str) -> Version:
        n = len(self.versions)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = None
        if self.draft:
            path = self.hist / f"{stamp}-v{n}.md"
            path.write_text(self.draft)
        v = Version(n, note, path, datetime.now().strftime("%H:%M:%S"))
        self.versions.append(v)
        with open(self.hist / "index.jsonl", "a") as f:
            f.write(json.dumps({"version": n, "note": note, "file": path.name if path else None, "at": stamp}) + "\n")
        return v

    def apply(self, text: str, note: str) -> str | None:
        """Write a new LARCH.md (kept as a version); an error message if it does not parse."""
        text = clean_draft(text)
        problem = check_draft(text, self.root)
        if problem:
            return problem
        self.draft = text
        self.md.write_text(self.draft)
        self._snapshot(note or "updated")
        return None

    def rollback(self, n: int) -> Version:
        """Restore version n (itself recorded as a new version, so a rollback can be undone)."""
        v = self.versions[n]
        self.draft = v.path.read_text() if v.path else ""
        if v.path:
            self.md.write_text(self.draft)
        elif self.md.exists():
            self.md.unlink()
        return self._snapshot(f"rolled back to v{n}")

    def undo(self) -> Version | None:
        return self.rollback(len(self.versions) - 2) if len(self.versions) > 1 else None

    def last_diff(self) -> str:
        if len(self.versions) < 2:
            return ""
        a, b = self.versions[-2], self.versions[-1]
        old = a.path.read_text() if a.path else ""
        new = b.path.read_text() if b.path else ""
        return "".join(difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                                            fromfile=f"v{a.n}", tofile=f"v{b.n}"))

    # -- conversation ------------------------------------------------------------------------
    def opening(self) -> str:
        if self.draft:
            return "(The developer opened Larch on a repository that already has the LARCH.md above. Greet briefly and ask what to work on.)"
        return "(The developer just opened Larch. Greet in one line, say what you see in the repository, and ask what it is for.)"

    def step(self, user_text: str | None, on_read=None) -> dict:
        """One exchange: the developer's message (None for the opening), and Larch's
        answer. Requests to read code are served and the model asked again (at most twice)."""
        if user_text is not None:
            self.turns.append(Turn("you", user_text))
        opening = self.opening() if user_text is None else None
        data: dict = {}
        for _ in range(3):
            data = self._ask(opening)
            reads = [r for r in data.get("read") or [] if isinstance(r, str)][:3]
            if not reads:
                break
            if on_read:
                on_read(reads)
            self.turns.append(Turn("code", read_sources(self.root, reads)))
        if data.get("draft"):
            problem = self.apply(str(data["draft"]), str(data.get("change", "")))
            if problem:
                data["say"] = (data.get("say") or "") + f"\n\n_(Larch kept the previous draft: {problem})_"
                data["draft"] = ""
        self.ready = bool(data.get("ready"))
        self.turns.append(Turn("larch", str(data.get("say", ""))))
        return data

    def _ask(self, opening: str | None) -> dict:
        prompt = "## Current LARCH.md\n" + (f"```markdown\n{self.draft}```" if self.draft else "(none yet)") + "\n\n"
        prompt += "## Conversation (most recent last)\n" + (render_turns(self.turns) or "(none)") + "\n"
        if opening:
            prompt += f"\n{opening}\n"
        resp = self.llm.complete(LLMRequest(system=SYSTEM + "\n## Repository\n" + self.summary, prompt=prompt,
                                            model=self.cfg.model, stage="chat", effort="low", max_tokens=4000,
                                            json_schema=CHAT_SCHEMA))
        return resp.data if isinstance(resp.data, dict) else {"say": resp.text, "choices": [], "read": [], "draft": "",
                                                              "change": "", "ready": False}


# ---------------------------------------------------------------------------
# Context, kept small
# ---------------------------------------------------------------------------

def repo_summary(root: Path, limit: int = 14) -> str:
    """The most promising functions and classes, one line each (what `larch scan` ranks)."""
    from .lang import language_for
    from .repo import scan

    try:
        cands = [c for c in scan([root], root=root) if c.status == "ready"][:limit]
    except Exception:  # noqa: BLE001
        cands = []
    lines = []
    for c in cands:
        doc = ""
        try:
            lang = language_for(root / c.file)
            info = (lang.extract_class if c.kind == "component" else lang.extract)(root / c.file, c.function)
            doc = (info.docstring or "").strip().splitlines()[0][:100] if info.docstring else ""
            sig = info.signature_text
        except Exception:  # noqa: BLE001
            sig = c.function
        lines.append(f"- {c.target} ({'class' if c.kind == 'component' else 'function'}): {sig}" + (f" — {doc}" if doc else ""))
    web = [p.relative_to(root) for p in root.rglob("*.py") if ".venv" not in p.parts and "node_modules" not in p.parts
           and any(k in p.read_text(errors="ignore")[:4000] for k in ("FastAPI(", "Flask(", "@app.route", "APIRouter("))][:3]
    if web:
        lines.append("Possible HTTP services: " + ", ".join(str(p) for p in web))
    return "\n".join(lines) or "(no verifiable functions found)"


def read_sources(root: Path, targets: list[str], limit: int = 2500) -> str:
    from .lang import language_for

    out = []
    for t in targets:
        path, _, name = t.partition("::")
        p = (root / path).resolve()
        try:
            if not p.is_relative_to(root.resolve()) or not p.is_file():
                raise FileNotFoundError(path)
            if name:
                lang = language_for(p)
                try:
                    src = lang.extract(p, name).source
                except Exception:  # noqa: BLE001
                    src = lang.extract_class(p, name).source
            else:
                src = p.read_text()
        except Exception as e:  # noqa: BLE001
            src = f"(could not read: {e})"
        out.append(f"### {t}\n```\n{src[:limit]}{' …(truncated)' if len(src) > limit else ''}\n```")
    return "\n".join(out)


def render_turns(turns: list[Turn], keep: int = 12) -> str:
    """The recent conversation; older turns are dropped (the draft carries what was agreed)."""
    recent = turns[-keep:]
    code_turns = [i for i, t in enumerate(recent) if t.who == "code"]
    out = []
    for i, t in enumerate(recent):
        if t.who == "code" and i != (code_turns[-1] if code_turns else -1):
            out.append("[code shown earlier]")
            continue
        out.append(f"{t.who.upper()}: {t.text}")
    return "\n\n".join(out)


def clean_draft(text: str) -> str:
    """The Markdown itself: no surrounding code fence, nothing before the first heading."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        text = text.rsplit("```", 1)[0] if text.rstrip().endswith("```") else text
    text = re.sub(r"^[^#\n]{1,3}(?=#)", "", text)  # stray characters glued to the first heading
    lines = text.splitlines()
    first = next((i for i, ln in enumerate(lines) if ln.startswith("#")), 0)
    return "\n".join(lines[first:]).strip() + "\n"


def check_draft(text: str, root: Path) -> str | None:
    """Why a draft is not a usable LARCH.md, or None. Parsed next to the real file so
    relative paths and `include:` resolve the same way."""
    p = root / f".{larchmd.FILENAME}.check"
    p.write_text(text)
    try:
        cf = larchmd.load(p)
    except larchmd.ContractsError as e:
        return str(e).replace(str(p), "LARCH.md")
    finally:
        p.unlink(missing_ok=True)
    missing = [s.target for s in cf.subjects if s.kind != "service" and not (root / s.target.split("::")[0]).exists()]
    return f"no such file: {', '.join(missing)}" if missing else None


# ---------------------------------------------------------------------------
# After verification: a summary to act on, or a certificate
# ---------------------------------------------------------------------------

def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""


def subject_files(root: Path, report: dict) -> list[Path]:
    files = [Path(report["file"])]
    if report.get("kind") == "service":
        md = root / larchmd.FILENAME
        try:
            subj = larchmd.load(md).subject(report["function"])
        except larchmd.ContractsError:
            subj = None
        src = (subj.settings.get("source") if subj else "") or ""
        files += [(Path(report["file"]).parent / s.strip()) for s in src.split(",") if s.strip()]
    return files


def certificate(root: Path, reports: list[dict]) -> dict:
    def git(*a):
        r = subprocess.run(["git", *a], cwd=root, capture_output=True, text=True)
        return r.stdout.strip() if r.returncode == 0 else ""

    subjects = []
    for r in reports:
        subjects.append({
            "subject": r["function"], "kind": r.get("kind"), "verdict": r["verdict"],
            "files": {str(Path(p).resolve().relative_to(root.resolve()) if Path(p).resolve().is_relative_to(root.resolve()) else p): _sha(Path(p))
                      for p in subject_files(root, r)},
            "contracts": [{"name": s["name"], "statement": s.get("english", ""), "proof": (s.get("proof") or {}).get("method", "")}
                          for s in r.get("specs", []) if s.get("approval") != "rejected"],
            "tested": (r.get("drt") or {}).get("valid", 0),
            "mutation_score": (r.get("mutation") or {}).get("score"),
            "proof_dir": r.get("artifacts_dir", ""),
        })
    cert = {
        "larch_certificate": 1, "issued": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "larch_version": __version__, "commit": git("rev-parse", "HEAD"), "worktree_clean": git("status", "--porcelain", "--", ".", ":!.larch", ":!LARCH-CERTIFICATE.json") == "",
        "larch_md_sha256": _sha(root / larchmd.FILENAME), "subjects": subjects,
    }
    cert["digest"] = hashlib.sha256(json.dumps(cert, sort_keys=True).encode()).hexdigest()
    return cert


def check_certificate(root: Path, cert: dict) -> list[str]:
    """What changed since the certificate was issued (empty: it still holds)."""
    problems = []
    body = {k: v for k, v in cert.items() if k != "digest"}
    if hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest() != cert.get("digest"):
        problems.append("the certificate itself was edited (digest mismatch)")
    if _sha(root / larchmd.FILENAME) != cert.get("larch_md_sha256"):
        problems.append("LARCH.md changed")
    for s in cert.get("subjects", []):
        for f, h in s.get("files", {}).items():
            if _sha((root / f) if not Path(f).is_absolute() else Path(f)) != h:
                problems.append(f"{f} changed ({s['subject']})")
    return problems


def summary_markdown(reports: list[dict]) -> str:
    out = ["# What to fix", "", f"_Larch, {datetime.now():%Y-%m-%d %H:%M}_", ""]
    for r in reports:
        if r["verdict"] == "passed":
            continue
        out += [f"## {r['function']} — {r['verdict']}", "", r.get("headline", ""), ""]
        for f in r.get("findings", []):
            out += [f"- **{f.get('kind', 'finding')}** ({f.get('confidence')}):", "", "  ```",
                    *("  " + ln for ln in str(f.get("args_repr", "")).splitlines()), "  ```",
                    f"  got `{str(f.get('impl'))[:200]}`, expected `{str(f.get('model'))[:200]}`.", ""]
            if f.get("explanation"):
                out += [f"  {f['explanation'].strip()}", ""]
            fix = f.get("fix") or {}
            if fix.get("diff"):
                out += [f"  Proposed fix ({'validated' if fix.get('validated') else 'not validated'}):", "", "  ```diff",
                        *("  " + ln for ln in fix["diff"].splitlines()), "  ```", ""]
        unproved = [s for s in r.get("specs", []) if (s.get("proof") or {}).get("status") == "unproved"]
        if unproved:
            out.append("Not proved (tested only):")
            out += [f"- `{s['name']}`: {s.get('english', '')}" for s in unproved]
            out.append("")
        if r.get("error"):
            out += [f"Error: {r['error']}", ""]
        out += [f"- {w}" for w in r.get("warnings", [])[:5]]
        out.append("")
    return "\n".join(out).rstrip() + "\n"


# ---------------------------------------------------------------------------
# The terminal UI
# ---------------------------------------------------------------------------

HELP = """\
[bold]/show[/]      the current LARCH.md        [bold]/diff[/]      what the last change did
[bold]/undo[/]      take back the last change   [bold]/rollback[/] pick any earlier version
[bold]/verify[/]    verify now                  [bold]/quit[/]      leave (LARCH.md stays as it is)"""


def _say(console: Console, text: str) -> None:
    if not text.strip():
        return
    console.print(Padding(Group(Text(f"{MARK} larch", style=f"bold {GOLD}"), Markdown(text)), (1, 0, 0, 1)))


def _note(console: Console, text: str) -> None:
    console.print(Padding(Text(text, style=MUTED), (0, 0, 0, 3)))


def _banner(console: Console, root: Path, model: str) -> None:
    t = Text.assemble((f" {MARK} ", f"bold {GOLD}"), ("larch", f"bold {GOLD}"), ("  contracts for your code, proved in Lean", MUTED))
    where = str(root).replace(str(Path.home()), "~")
    where = where if len(where) <= 50 else "…" + where[-49:]
    console.print(Panel(Group(t, Text(f"   {where}  ·  {model}", style=MUTED)), border_style=BARK, padding=(0, 1), expand=False))
    _note(console, "Tell me what the code is for; I'll write the contracts and check them with you. /help for commands.")


def _choose(question: str, options: list[str]) -> str | None:
    import questionary

    style = questionary.Style([("qmark", f"fg:{GOLD} bold"), ("pointer", f"fg:{GOLD} bold"), ("highlighted", f"fg:{GOLD} bold"),
                               ("selected", f"fg:{NEEDLE}"), ("question", "bold")])
    return questionary.select(question, choices=options, qmark=MARK, pointer="›", style=style).ask()


def run(root: Path | None = None, *, cfg: Config | None = None, llm: LLM | None = None, console: Console | None = None) -> int:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.formatted_text import HTML
    from prompt_toolkit.history import FileHistory
    from prompt_toolkit.styles import Style

    from .llm.providers import make_provider
    from .util import repo_root

    console = console or Console(highlight=False)
    root = repo_root(Path(root or Path.cwd()).resolve())
    cfg = cfg or Config.load(root)
    ledger = llm.ledger if llm else Ledger(cfg.budget_usd)
    llm = llm or LLM(make_provider(cfg.provider, cache=cfg.cache), ledger)
    if not sys.stdin.isatty():
        console.print("larch: the interactive session needs a terminal. Use `larch verify` in scripts.")
        return 2
    _banner(console, root, cfg.model)
    session = ChatSession(root, cfg, llm)
    with console.status(f"[{MUTED}]reading the repository…", spinner="dots"):
        session.summary = repo_summary(root)

    (root / ".larch").mkdir(exist_ok=True)
    ps = PromptSession(history=FileHistory(str(root / ".larch" / "chat_history")))
    style = Style.from_dict({"bottom-toolbar": f"noreverse bg:default fg:{MUTED}", "prompt": f"fg:{NEEDLE} bold"})

    def toolbar():
        v = session.versions[-1].n
        return HTML(f" LARCH.md v{v} · ${ledger.spent():.3f} · /help · ctrl-d to leave")

    def exchange(text: str | None) -> dict:
        with console.status(f"[{MUTED}]thinking…", spinner="dots") as st:
            try:
                data = session.step(text, on_read=lambda r: st.update(f"[{MUTED}]reading {', '.join(r)}…"))
            except LLMError as e:
                console.print(f"[red]LLM error:[/] {e}")
                return {}
        _say(console, data.get("say", ""))
        if data.get("draft"):
            _note(console, f"✎ LARCH.md v{session.versions[-1].n}: {data.get('change') or 'updated'}   (/diff to see, /undo to take back)")
        return data

    data = exchange(None)
    while True:
        choices = [c for c in data.get("choices") or [] if isinstance(c, str)][:5]
        if session.ready:
            ans = _choose("Start verifying?", ["Yes, verify now", "Not yet, keep refining", "Leave for now"])
            if ans is None or ans.startswith("Leave"):
                break
            if ans.startswith("Yes"):
                return verify_phase(root, cfg, console)
            session.ready = False
            text = "Not yet; let's keep refining."
        elif choices:
            ans = _choose("", choices + ["Something else…"])
            if ans is None:
                break
            text = ans if ans != "Something else…" else None
            if text is None:
                text = _prompt(ps, style, toolbar)
                if text is None:
                    break
        else:
            text = _prompt(ps, style, toolbar)
            if text is None:
                break
        text = text.strip()
        if not text:
            data = {}
            continue
        if text.startswith("/"):
            cmd = text.split()[0].lower()
            data = {}
            if cmd in ("/quit", "/exit"):
                break
            if cmd == "/help":
                console.print(Padding(Text.from_markup(HELP), (1, 0, 0, 3)))
            elif cmd == "/show":
                console.print(Padding(Syntax(session.draft or "(empty)", "markdown", theme="ansi_dark", background_color="default"), (1, 0, 0, 3)))
            elif cmd == "/diff":
                d = session.last_diff()
                console.print(Padding(Syntax(d, "diff", theme="ansi_dark", background_color="default") if d else Text("no changes yet", style=MUTED), (1, 0, 0, 3)))
            elif cmd == "/undo":
                v = session.undo()
                _note(console, f"↶ restored the previous version (now v{v.n})" if v else "nothing to undo")
            elif cmd == "/rollback":
                opts = [f"v{v.n}  {v.at}  {v.note}" for v in session.versions]
                pick = _choose("Roll back to", list(reversed(opts)))
                if pick:
                    v = session.rollback(int(pick.split()[0][1:]))
                    _note(console, f"↶ LARCH.md is now {pick.split()[0]}'s content (recorded as v{v.n}; /undo reverts)")
            elif cmd == "/verify":
                return verify_phase(root, cfg, console)
            else:
                _note(console, f"unknown command {cmd}; /help lists them")
            continue
        data = exchange(text)
    state = f"LARCH.md is saved (v{session.versions[-1].n})" if session.md.exists() else "There is no LARCH.md (as before the session)"
    _note(console, f"{state}. Every version is kept in .larch/history/; run `larch` again any time.")
    return 0


def _prompt(ps, style, toolbar) -> str | None:
    from prompt_toolkit.formatted_text import HTML

    try:
        return ps.prompt(HTML("\n<prompt>› </prompt>"), style=style, bottom_toolbar=toolbar)
    except (EOFError, KeyboardInterrupt):
        return None


def verify_phase(root: Path, cfg: Config, console: Console) -> int:
    """Run `larch verify` on LARCH.md (its live progress is the log), then summarize."""
    import os

    from .cli import build_parser, cmd_verify

    if not (root / larchmd.FILENAME).exists():
        _note(console, "There is no LARCH.md to verify yet.")
        return 3
    console.print(Rule(Text(f"{MARK} verifying", style=f"bold {GOLD}"), style=BARK))
    (root / ".larch").mkdir(exist_ok=True)
    out = root / ".larch" / f"run-{datetime.now():%Y%m%d-%H%M%S}.json"
    cwd = Path.cwd()
    os.chdir(root)
    try:
        args = build_parser().parse_args(["verify", "--yes", "--json", str(out)])
        t0 = time.monotonic()
        code = cmd_verify(args, console)
    finally:
        os.chdir(cwd)
    if not out.exists():
        _note(console, "Verification produced no report.")
        return code
    reports = json.loads(out.read_text())
    reports = reports if isinstance(reports, list) else [reports]
    console.print(Rule(style=BARK))
    if reports and all(r["verdict"] == "passed" for r in reports):
        cert = certificate(root, reports)
        path = root / "LARCH-CERTIFICATE.json"
        path.write_text(json.dumps(cert, indent=2) + "\n")
        show_certificate(console, cert, path, time.monotonic() - t0)
        return 0
    md = root / ".larch" / "what-to-fix.md"
    md.write_text(summary_markdown(reports))
    t = Table(box=None, show_header=False, padding=(0, 2))
    for r in reports:
        mark = {"passed": ("✓", NEEDLE), "bug": ("✗", "red"), "partial": ("◐", GOLD)}.get(r["verdict"], ("!", "red"))
        t.add_row(Text(mark[0], style=mark[1]), Text(r["function"], style="bold"), Text(r.get("headline", "")[:90], style=MUTED))
    console.print(Panel(Group(Text("Not certified yet. What to fix:", style="bold"), Text(""), t, Text(""),
                              Text(f"Full summary with failing calls and proposed fixes: {md}", style=MUTED)),
                        title=Text(f" {MARK} larch ", style=f"bold {GOLD}"), border_style=BARK, padding=(1, 2)))
    return code or 1


def show_certificate(console: Console, cert: dict, path: Path, elapsed: float) -> None:
    rows = Table(box=None, show_header=False, padding=(0, 2))
    for s in cert["subjects"]:
        proved = sum(1 for c in s["contracts"] if c["proof"])
        rows.add_row(Text("✓", style=NEEDLE), Text(s["subject"], style="bold"),
                     Text(f"{proved} contract{'s' if proved != 1 else ''} proved · {s['tested']:,} tests", style=MUTED))
    body = Group(
        Text("CERTIFICATE OF VERIFICATION", style=f"bold {GOLD}", justify="center"),
        Text(""), Align.center(rows), Text(""),
        Text(f"commit {cert['commit'][:12] or '—'}{'' if cert['worktree_clean'] else ' (uncommitted changes)'}  ·  "
             f"LARCH.md {cert['larch_md_sha256'][:12]}  ·  {cert['issued']}", style=MUTED, justify="center"),
        Text(f"digest {cert['digest']}", style=MUTED, justify="center"),
        Text(""),
        Text(f"Saved to {path.name} after {elapsed / 60:.0f} min" if elapsed >= 60 else f"Saved to {path.name} after {elapsed:.0f} s",
             style=MUTED, justify="center"),
        Text("`larch certificate` checks that it still matches the code.",
             style=MUTED, justify="center"),
    )
    console.print(Panel(body, title=Text(f" {MARK} larch ", style=f"bold {GOLD}"), border_style=GOLD, box=box.DOUBLE, padding=(1, 4)))
