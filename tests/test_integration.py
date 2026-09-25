"""Integration tests: real Lean toolchain and real worker, scripted (fake) LLM.

These take ~1-2 minutes in total. Skipped automatically if the pinned Lean
toolchain is not installed.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from larch.config import Config
from larch.lean.checker import Checker
from larch.lean.toolchain import ToolchainError, find_toolchain
from larch.lean.workspace import LeanWorkspace
from larch.llm.base import LLM, Ledger
from larch.llm.providers import FakeProvider
from larch.spec import FormalSpec, Param, Postcondition, model_module, proof_module, spec_constant, theorem_name

try:
    TC = find_toolchain()
except ToolchainError:  # pragma: no cover
    TC = None

pytestmark = pytest.mark.skipif(TC is None, reason="pinned Lean toolchain not installed")


def _clamp_spec() -> FormalSpec:
    return FormalSpec(
        function="clamp", understanding="", params=[Param("x", "Int"), Param("lo", "Int"), Param("hi", "Int")],
        return_type="Int", exceptions=False,
        model_code="def model (x lo hi : Int) : Int := if x < lo then lo else if x > hi then hi else x",
        pre_english="lo ≤ hi", pre_lean="lo ≤ hi",
        postconditions=[Postcondition("in_range", "in range", "lo ≤ result ∧ result ≤ hi")],
    )


@pytest.fixture(scope="module")
def ws(tmp_path_factory):
    w = LeanWorkspace(tmp_path_factory.mktemp("lean"), TC)
    r = w.compile_module(w.MODEL, model_module(_clamp_spec()))
    assert r.ok, r.error_text()
    return w


CHEATS = {
    "sorry": "theorem in_range_holds : spec_in_range := by\n  sorry",
    "native_decide_axiom": (
        "theorem aux : (2 : Nat) + 2 = 4 := by decide\n"
        "axiom cheat : spec_in_range\n"
        "theorem in_range_holds : spec_in_range := cheat"
    ),
    "weakened_statement": "theorem in_range_holds : True := trivial",
    "instance_hijack": (
        # Tries to redefine ≤ on Int *after* the spec was elaborated. The spec constant
        # is fixed, so this cannot change what is being proved.
        "instance weird : LE Int := ⟨fun _ _ => True⟩\n"
        "theorem in_range_holds : spec_in_range := by\n  intro x lo hi h\n  exact ⟨trivial, trivial⟩"
    ),
}


def test_checker_accepts_real_proof(ws):
    body = "theorem in_range_holds : spec_in_range := by\n  intro x lo hi h\n  simp only [pre, post_in_range] at *\n  grind [model]"
    r = ws.compile_module("ProofOK", proof_module("in_range", body))
    assert r.ok, r.error_text()
    [chk] = Checker(TC).check("ProofOK", [(theorem_name("in_range"), spec_constant("in_range"))], lean_path=ws.lean_path, cwd=ws.root)
    assert chk.accepted, chk.reason()
    assert set(chk.axioms) <= {"propext", "Classical.choice", "Quot.sound"}


@pytest.mark.parametrize("name", sorted(CHEATS))
def test_checker_rejects_cheats(ws, name):
    mod = f"Cheat_{name}"
    r = ws.compile_module(mod, proof_module("in_range", CHEATS[name]))
    if not r.ok:
        return  # Lean itself refused: also a rejection
    [chk] = Checker(TC).check(mod, [(theorem_name("in_range"), spec_constant("in_range"))], lean_path=ws.lean_path, cwd=ws.root, replay=False)
    assert not chk.accepted, f"{name} was accepted!"


# -- full pipeline with a scripted LLM ---------------------------------------------------------------

CLAMP_OK = '''\
def clamp(x: int, lo: int, hi: int) -> int:
    """Clamp x into [lo, hi]; requires lo <= hi."""
    if x < lo:
        return lo
    if x > hi:
        return hi
    return x
'''

CLAMP_BUG = CLAMP_OK.replace("return hi", "return hi + 1")

FORMALIZATION = {
    "understanding": "Clamp x into the closed interval [lo, hi].",
    "params": [{"name": "x", "lean_type": "Int"}, {"name": "lo", "lean_type": "Int"}, {"name": "hi", "lean_type": "Int"}],
    "return_type": "Int",
    "exceptions": False,
    "model": "def model (x lo hi : Int) : Int :=\n  if x < lo then lo else if x > hi then hi else x",
    "precondition": {"english": "lo ≤ hi", "lean": "lo ≤ hi"},
    "postconditions": [
        {"name": "in_range", "english": "The result lies in [lo, hi].", "lean": "lo ≤ result ∧ result ≤ hi"},
        {"name": "identity_inside", "english": "Inside values are unchanged.", "lean": "lo ≤ x → x ≤ hi → result = x"},
    ],
    "properties": [
        {"name": "idempotent", "english": "Clamping twice is the same as once.",
         "params": [{"name": "x", "lean_type": "Int"}, {"name": "lo", "lean_type": "Int"}, {"name": "hi", "lean_type": "Int"}],
         "lean": "lo ≤ hi → model (model x lo hi) lo hi = model x lo hi"},
    ],
    "strategy": "def strategy(st):\n    return st.tuples(st.integers(-50, 50), st.integers(-50, 50), st.integers(0, 20)).map(lambda t: (t[0], t[1], t[1] + t[2]))",
    "edge_cases": "[[0, 0, 0], [5, 1, 3], [-7, -3, 2]]",
    "notes": "",
}


def fake_llm():
    def respond(req):
        if req.stage == "formalize":
            return FORMALIZATION
        if req.stage == "adjudicate":
            return {"verdict": "implementation_bug", "explanation": "returns hi + 1 above the interval"}
        if req.stage == "fix":
            return {"explanation": "return hi, not hi + 1", "fixed_function": CLAMP_OK}
        return "```lean\n-- nothing\n```"

    return LLM(FakeProvider(respond), Ledger())


def _cfg(tmp_path: Path) -> Config:
    return Config(auto_approve=True, tests=400, mutants=15, mutation_tests=150, artifacts=str(tmp_path / "runs"), cache=False)


def test_pipeline_passes_correct_function(tmp_path: Path):
    from larch.engine.session import verify_function

    f = tmp_path / "clamp.py"
    f.write_text(CLAMP_OK)
    report = verify_function(f, "clamp", _cfg(tmp_path), llm=fake_llm())
    assert report.verdict == "passed", (report.error, report.headline)
    assert report.proved == 3
    assert report.drt["disagreements"] == 0 and report.drt["valid"] > 100
    assert report.mutation and report.mutation.total > 0 and report.mutation.killed > 0
    saved = json.loads((Path(report.artifacts_dir) / "report.json").read_text())
    assert saved["verdict"] == "passed"
    # user code untouched
    assert f.read_text() == CLAMP_OK


def test_pipeline_finds_bug_and_validates_fix(tmp_path: Path):
    from larch.engine.session import verify_function

    f = tmp_path / "clamp.py"
    f.write_text(CLAMP_BUG)
    report = verify_function(f, "clamp", _cfg(tmp_path), llm=fake_llm())
    assert report.verdict == "bug", report.headline
    assert report.bug_reported
    top = report.findings[0]
    assert top.confidence == "confirmed" and "in_range" in top.violated_specs
    assert top.fix is not None and top.fix.validated
    assert "-        return hi + 1" in top.fix.diff and "+        return hi" in top.fix.diff
    assert f.read_text() == CLAMP_BUG  # read-only: the fix is only proposed


# -- exceptions are modelled as `none` -----------------------------------------------------------

DIV_OK = '''\
def safe_div(a: int, b: int) -> int:
    """Floor-divide a by b. Raises ZeroDivisionError when b == 0."""
    if b == 0:
        raise ZeroDivisionError("b is zero")
    return a // b
'''

DIV_BUG = DIV_OK.replace('raise ZeroDivisionError("b is zero")', "return 0")

DIV_FORMALIZATION = {
    "understanding": "Floor division that raises on a zero divisor.",
    "params": [{"name": "a", "lean_type": "Int"}, {"name": "b", "lean_type": "Int"}],
    "return_type": "Int",
    "exceptions": True,
    "model": "def model (a b : Int) : Option Int :=\n  if b = 0 then none else some (Int.fdiv a b)",
    "precondition": {"english": "none", "lean": "True"},
    "postconditions": [
        {"name": "raises_iff_zero", "english": "Raises exactly when b is zero.", "lean": "result = none ↔ b = 0"},
        {"name": "floor_quotient", "english": "Otherwise returns the floor quotient.", "lean": "∀ q ∈ result, b * q ≤ a ∧ a < b * q + b ∨ b < 0"},
    ],
    "properties": [],
    "strategy": "def strategy(st):\n    return st.tuples(st.integers(-100, 100), st.integers(-5, 5))",
    "edge_cases": "[[7, 0], [-7, 2], [7, -2]]",
    "notes": "",
}


def fake_div_llm():
    def respond(req):
        if req.stage == "formalize":
            return DIV_FORMALIZATION
        if req.stage == "adjudicate":
            return {"verdict": "implementation_bug", "explanation": "must raise on zero"}
        if req.stage == "fix":
            return {"explanation": "raise on zero", "fixed_function": DIV_OK}
        return "```lean\n-- nothing\n```"

    return LLM(FakeProvider(respond), Ledger())


def test_exceptions_modelled_as_none(tmp_path: Path):
    from larch.engine.session import verify_function

    cfg = _cfg(tmp_path)
    cfg.run_mutation = False
    f = tmp_path / "safe_div.py"
    f.write_text(DIV_OK)
    ok = verify_function(f, "safe_div", cfg, llm=fake_div_llm())
    assert ok.verdict in ("passed", "partial"), (ok.error, ok.headline)
    assert ok.drt["disagreements"] == 0 and ok.drt["valid"] > 50
    f.write_text(DIV_BUG)
    bad = verify_function(f, "safe_div", cfg, llm=fake_div_llm())
    assert bad.verdict == "bug", bad.headline
    assert any("raises_iff_zero" in fi.violated_specs for fi in bad.findings)


# -- approved specs are persisted and reused (CI workflow) ------------------------------------------

def test_approved_spec_is_reused_without_llm(tmp_path: Path, monkeypatch):
    from larch.engine.session import verify_function
    from larch.ui import UI

    monkeypatch.setenv("LARCH_CACHE_DIR", str(tmp_path / "cache"))
    (tmp_path / "proj" / ".larch").mkdir(parents=True)  # `larch init` opt-in
    f = tmp_path / "proj" / "clamp.py"
    f.write_text(CLAMP_OK)
    cfg = _cfg(tmp_path)
    cfg.auto_approve = False  # the (silent) UI approves, like a person pressing [a]
    cfg.run_mutation = False
    llm = fake_llm()
    first = verify_function(f, "clamp", cfg, UI(), llm=llm)
    assert first.verdict == "passed", first.error
    saved = list((tmp_path / "proj" / ".larch" / "specs").rglob("clamp.json"))
    assert saved, "approved spec was not written to .larch/specs"
    n_formalize = sum(1 for r in llm.provider.requests if r.stage == "formalize")
    second = verify_function(f, "clamp", cfg, UI(), llm=llm)
    assert second.verdict == "passed", second.error
    assert sum(1 for r in llm.provider.requests if r.stage == "formalize") == n_formalize  # no new formalization
