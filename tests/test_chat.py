"""The interactive session: drafting LARCH.md, rollback, the summary and the certificate."""
from __future__ import annotations

import json
from pathlib import Path

from larch.chat import ChatSession, certificate, check_certificate, render_turns, summary_markdown, Turn
from larch.config import Config
from larch.llm.base import LLM, Ledger
from larch.llm.providers import FakeProvider

SRC = 'def add(a: int, b: int) -> int:\n    """Sum of two integers."""\n    return a + b\n'
DRAFT = "# Contracts\n\n## calc.py::add\n- Adding zero changes nothing.\n"


def _repo(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    (tmp_path / "calc.py").write_text(SRC)
    return tmp_path


def _session(tmp_path: Path, replies: list[dict]) -> tuple[ChatSession, list]:
    prompts = []

    def respond(req):
        prompts.append(req.prompt)
        base = {"say": "", "choices": [], "read": [], "draft": "", "change": "", "ready": False}
        return {**base, **replies.pop(0)}

    s = ChatSession(_repo(tmp_path), Config(), LLM(FakeProvider(respond), Ledger()))
    s.summary = "- calc.py::add (function): def add(a: int, b: int) -> int"
    return s, prompts


def test_reads_code_then_drafts_and_versions(tmp_path):
    s, prompts = _session(tmp_path, [
        {"say": "", "read": ["calc.py::add"]},
        {"say": "So adding zero changes nothing. Right?", "choices": ["Yes", "Adjust"], "draft": DRAFT, "change": "add: identity"},
    ])
    reads = []
    data = s.step(None, on_read=reads.append)
    assert reads == [["calc.py::add"]]
    assert "return a + b" in prompts[1] and "just opened Larch" in prompts[1]  # source shown; opening kept
    assert data["choices"] == ["Yes", "Adjust"]
    assert (tmp_path / "LARCH.md").read_text() == DRAFT
    assert [v.note for v in s.versions] == ["no LARCH.md yet", "add: identity"]


def test_draft_is_cleaned(tmp_path):
    s, _ = _session(tmp_path, [])
    assert s.apply("```markdown\n" + DRAFT + "```", "fenced") is None
    assert (tmp_path / "LARCH.md").read_text() == DRAFT
    assert s.apply("1" + DRAFT, "junk first") is None
    assert (tmp_path / "LARCH.md").read_text() == DRAFT


def test_invalid_draft_is_refused(tmp_path):
    s, _ = _session(tmp_path, [{"say": "Here.", "draft": "# Contracts\n\n## nope.py::f\n- x\n"}])
    data = s.step("hi")
    assert "kept the previous draft" in data["say"] and "nope.py" in data["say"]
    assert not (tmp_path / "LARCH.md").exists() and len(s.versions) == 1


def test_undo_and_rollback_restore_any_version(tmp_path):
    s, _ = _session(tmp_path, [])
    assert s.apply(DRAFT, "first") is None
    second = DRAFT + "- Order does not matter.\n"
    assert s.apply(second, "second") is None
    s.undo()
    assert (tmp_path / "LARCH.md").read_text() == DRAFT
    s.rollback(0)  # before the session there was no LARCH.md
    assert not (tmp_path / "LARCH.md").exists()
    s.rollback(2)
    assert (tmp_path / "LARCH.md").read_text() == second
    assert "Order does not matter" in s.last_diff()
    index = [json.loads(x) for x in (tmp_path / ".larch" / "history" / "index.jsonl").read_text().splitlines()]
    assert [e["note"] for e in index][-1] == "rolled back to v2"


def test_old_code_is_elided_from_the_prompt():
    turns = [Turn("code", "OLD SOURCE"), Turn("you", "hm"), Turn("code", "NEW SOURCE")]
    text = render_turns(turns)
    assert "OLD SOURCE" not in text and "NEW SOURCE" in text


def _report(tmp_path: Path, verdict: str) -> dict:
    return {"function": "add", "file": str(tmp_path / "calc.py"), "kind": "function", "verdict": verdict,
            "headline": "Implementation disagrees with the verified model.", "drt": {"valid": 300},
            "specs": [{"name": "identity", "english": "Adding zero changes nothing.", "approval": "approved",
                       "proof": {"status": "proved" if verdict == "passed" else "unproved", "method": "auto: grind"}}],
            "findings": [] if verdict == "passed" else [{"kind": "divergence", "confidence": "likely", "args_repr": "1, 0",
                                                        "impl": "2", "model": "1", "explanation": "off by one",
                                                        "fix": {"diff": "-    return a + b + 1\n+    return a + b", "validated": True}}]}


def test_summary_lists_what_to_fix(tmp_path):
    md = summary_markdown([_report(tmp_path, "bug"), _report(tmp_path, "passed")])
    assert "## add — bug" in md and "got `2`, expected `1`" in md and "off by one" in md
    assert "Proposed fix (validated)" in md and "Not proved" in md
    assert md.count("## add") == 1  # passed subjects are left out


def test_certificate_pins_contracts_and_sources(tmp_path):
    root = _repo(tmp_path)
    (root / "LARCH.md").write_text(DRAFT)
    cert = certificate(root, [_report(root, "passed")])
    assert cert["subjects"][0]["files"] == {"calc.py": cert["subjects"][0]["files"]["calc.py"]}
    assert cert["subjects"][0]["contracts"][0]["proof"] == "auto: grind"
    assert check_certificate(root, cert) == []
    (root / "calc.py").write_text(SRC.replace("a + b", "b + a"))
    assert check_certificate(root, cert) == ["calc.py changed (add)"]
    forged = dict(cert, issued="2000-01-01")
    assert "the certificate itself was edited (digest mismatch)" in check_certificate(root, forged)
