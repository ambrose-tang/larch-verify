"""LARCH.md: the project's contracts, in plain English (optionally with Lean).

One file at the repository root says what the code must do:

    # Contracts

    ## shop/shipping.py::shipping_cost_cents          <- a function
    - The cost is never negative.
    - Express costs more than standard for the same parcel.

    ## shop/ledger.py::Ledger                          <- a class (stateful component)
    - The balance never goes negative.

    ## service orders                                  <- a service
    start: uvicorn shop.api:app --port {port}
    - GET /orders/{id} is 404 for an id that was never created.

    # System rules
    - A customer is never charged twice for one order.  (uses: Ledger, service orders)

A bullet may be followed by a fenced ```lean block giving its exact statement; the
English is then documentation and the Lean is used verbatim. `key: value` lines under a
heading configure the subject. `include: path.md` at the top level pulls in another
file (paths relative to the including file). A heading with no bullets asks Larch to
propose the contracts; once a person approves them they are written back as bullets.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .util import repo_root

FILENAME = "LARCH.md"

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_BULLET = re.compile(r"^[-*+]\s+(.*)$")
_SETTING = re.compile(r"^([a-z][a-z0-9_-]*)\s*:\s*(.*)$")
_USES = re.compile(r"\(\s*uses\s*:\s*([^)]*)\)\s*$", re.I)


class ContractsError(ValueError):
    pass


@dataclass
class Contract:
    text: str  # the developer's English
    lean: str | None = None  # an exact Lean statement, if given
    line: int = 0  # 1-based line in the file
    uses: list[str] = field(default_factory=list)  # system rules: the subjects they rely on

    def to_json(self) -> dict:
        return {"text": self.text, "lean": self.lean, "line": self.line, "uses": self.uses}


@dataclass
class Subject:
    kind: str  # function | component | service
    target: str  # "path::name" or the service name
    file: Path  # LARCH.md (or included file) that declares it
    line: int
    settings: dict[str, str] = field(default_factory=dict)
    contracts: list[Contract] = field(default_factory=list)
    path: Path | None = None  # source file (functions, components)
    name: str = ""  # function / class name, or service name

    @property
    def label(self) -> str:
        return f"service {self.name}" if self.kind == "service" else self.target


@dataclass
class ContractFile:
    path: Path
    root: Path
    subjects: list[Subject] = field(default_factory=list)
    rules: list[Contract] = field(default_factory=list)
    settings: dict[str, str] = field(default_factory=dict)

    def subject(self, label: str) -> Subject | None:
        label = label.strip()
        for s in self.subjects:
            if label in (s.label, s.target, s.name, f"service {s.name}"):
                return s
        return None

    def for_function(self, path: Path, name: str) -> Subject | None:
        p = Path(path).resolve()
        for s in self.subjects:
            if s.path is not None and s.path.resolve() == p and s.name == name:
                return s
        return None


def find(start: Path | None = None) -> Path | None:
    """LARCH.md at the repository root (or the nearest one above `start`)."""
    start = Path(start or Path.cwd()).resolve()
    top = repo_root(start)
    d = start if start.is_dir() else start.parent
    for cand in [d, *d.parents]:
        if (cand / FILENAME).is_file():
            return cand / FILENAME
        if cand == top:
            break
    return None


def load(path: Path) -> ContractFile:
    path = Path(path).resolve()
    cf = ContractFile(path=path, root=path.parent)
    _parse_into(cf, path, seen=set())
    return cf


def _parse_into(cf: ContractFile, path: Path, seen: set[Path]) -> None:
    if path in seen:
        raise ContractsError(f"{path}: included twice (include cycle?)")
    seen.add(path)
    try:
        lines = path.read_text().splitlines()
    except OSError as e:
        raise ContractsError(f"cannot read {path}: {e}") from e
    section = "other"  # contracts | rules | other (preamble)
    subject: Subject | None = None
    last: Contract | None = None
    i = 0
    while i < len(lines):
        raw = lines[i]
        line = raw.strip()
        lineno = i + 1
        i += 1
        if not line or line.startswith("<!--"):
            if line.startswith("<!--") and "-->" not in line:
                while i < len(lines) and "-->" not in lines[i]:
                    i += 1
                i += 1
            continue
        if line.startswith("```"):
            fence = line[3:].strip().lower()
            body = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                body.append(lines[i])
                i += 1
            i += 1  # closing fence
            if fence == "lean":
                if last is None:
                    raise ContractsError(f"{path}:{lineno}: a ```lean block must follow a contract bullet")
                last.lean = "\n".join(body).strip()
            continue
        m = _HEADING.match(line)
        if m:
            level, title = len(m.group(1)), m.group(2).strip()
            last = None
            if level == 1:
                t = title.lower()
                section = "rules" if "rule" in t or "system" in t else ("contracts" if "contract" in t else "other")
                subject = None
                continue
            is_subject = "::" in title or title.lower().strip("`").startswith("service ")
            if section == "rules" or (section == "other" and not is_subject):
                subject = None  # grouping or documentation headings
                continue
            subject = _subject(title, path, lineno, cf.root)
            cf.subjects.append(subject)
            section = "contracts"
            continue
        b = _BULLET.match(line)
        if b:
            text = b.group(1).strip()
            while i < len(lines) and lines[i].startswith(("  ", "\t")) and lines[i].strip() \
                    and not _BULLET.match(lines[i].strip()) and not lines[i].strip().startswith("```"):
                text += " " + lines[i].strip()
                i += 1
            c = Contract(text=text, line=lineno)
            u = _USES.search(text)
            if u:
                c.uses = [x.strip() for x in u.group(1).split(",") if x.strip()]
                c.text = text[: u.start()].rstrip()
            if section == "rules":
                cf.rules.append(c)
                last = c
            elif subject is not None:
                subject.contracts.append(c)
                last = c
            # bullets before any subject heading are prose for people
            continue
        s = _SETTING.match(line)
        if s:
            key, val = s.group(1).lower(), s.group(2).strip().strip("`")
            if key == "include":
                _parse_into(cf, (path.parent / val).resolve(), seen)
                subject, last = None, None
            elif subject is None:
                cf.settings[key] = val
            else:
                subject.settings[key] = val
            continue
        # other prose: ignored (it is documentation for people)


def _subject(title: str, file: Path, line: int, root: Path) -> Subject:
    title = title.strip().strip("`")
    low = title.lower()
    if low.startswith("service "):
        name = title.split(None, 1)[1].strip()
        return Subject(kind="service", target=f"service {name}", file=file, line=line, name=name)
    if "::" not in title:
        raise ContractsError(f"{file}:{line}: subject heading must be `path/to/file::name` or `service NAME`, got {title!r}")
    rel, name = title.split("::", 1)
    src = (root / rel).resolve()
    kind = "component" if _looks_like_class(src, name) else "function"
    return Subject(kind=kind, target=f"{rel}::{name}", file=file, line=line, path=src, name=name)


def _looks_like_class(src: Path, name: str) -> bool:
    if "." in name or not src.exists():
        return False
    try:
        text = src.read_text(errors="replace")
    except OSError:
        return False
    return re.search(rf"^\s*(export\s+)?(default\s+)?(abstract\s+)?class\s+{re.escape(name)}\b", text, re.M) is not None


# ---------------------------------------------------------------------------
# Writing back approved contracts, and drafting a new file
# ---------------------------------------------------------------------------

def write_back(subject: Subject, bullets: list[str]) -> bool:
    """Add approved contracts under a subject heading that had none. Returns whether the
    file was changed (never rewrites existing bullets)."""
    if subject.contracts or not bullets:
        return False
    lines = subject.file.read_text().splitlines()
    idx = subject.line  # line after the heading (0-based index == 1-based line)
    while idx < len(lines) and _SETTING.match(lines[idx].strip()):
        idx += 1
    insert = [f"- {b.strip()}" for b in bullets]
    lines[idx:idx] = insert
    subject.file.write_text("\n".join(lines) + "\n")
    subject.contracts = [Contract(text=b.strip(), line=subject.line + 1 + k) for k, b in enumerate(bullets)]
    return True


TEMPLATE = """\
# LARCH.md: what this code must do

Larch checks every contract below: it writes a Lean model of each subject, proves the
contracts about the model, and tests the real code against the model (every input,
when the inputs are few enough).

- Write contracts in plain English, one per bullet. A ```lean block under a bullet
  states it exactly.
- A heading with no bullets asks Larch to propose contracts; the ones you approve are
  written back here.
- Run `larch verify` to check everything here, or `larch verify --changed` in CI.

# Contracts
{subjects}
# System rules
<!-- Rules that span several subjects, e.g.:
- A customer is never charged twice for one order.  (uses: shop/ledger.py::Ledger, service orders)
-->
"""


def draft(root: Path, targets: list[str]) -> str:
    body = "".join(f"\n## {t}\n" for t in targets) or "\n<!-- ## path/to/file.py::function -->\n"
    return TEMPLATE.format(subjects=body + "\n")
