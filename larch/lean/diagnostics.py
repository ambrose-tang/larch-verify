"""Parse `lean` command-line output into structured messages."""
from __future__ import annotations

import re
from dataclasses import dataclass

_HEADER = re.compile(
    r"^(?P<file>[^\n:]+?):(?P<line>\d+):(?P<col>\d+): (?P<sev>error|warning|info)(?:\((?P<code>[^)]*)\))?: ?(?P<msg>.*)$"
)


@dataclass
class Message:
    file: str
    line: int
    col: int
    severity: str
    text: str
    code: str | None = None

    def render(self, line_offset: int = 0) -> str:
        return f"line {self.line - line_offset}:{self.col}: {self.severity}: {self.text}"


def parse_messages(output: str) -> list[Message]:
    msgs: list[Message] = []
    cur: Message | None = None
    for raw in output.splitlines():
        m = _HEADER.match(raw)
        if m:
            cur = Message(
                file=m.group("file"),
                line=int(m.group("line")),
                col=int(m.group("col")),
                severity=m.group("sev"),
                text=m.group("msg"),
                code=m.group("code"),
            )
            msgs.append(cur)
        elif cur is not None:
            cur.text += "\n" + raw
        elif raw.strip():
            # Output before any header (e.g. a crash); keep as a pseudo error.
            cur = Message(file="", line=0, col=0, severity="error", text=raw)
            msgs.append(cur)
    for msg in msgs:
        msg.text = msg.text.rstrip()
    return msgs


def errors(msgs: list[Message]) -> list[Message]:
    return [m for m in msgs if m.severity == "error"]


def mentions_sorry(msgs: list[Message]) -> bool:
    return any("declaration uses 'sorry'" in m.text or "declaration uses `sorry`" in m.text for m in msgs)
