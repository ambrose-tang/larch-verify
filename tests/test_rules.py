"""System rules: a guarantee proved from component contracts (assume–guarantee)."""
from __future__ import annotations

import json
from pathlib import Path


from larch.component import ComponentSpec, Observer, Operation, StepContract
from larch.config import Config
from larch.contracts import Contract, Subject
from larch.report import ProofResult, Report, SpecResult
from larch.spec import Param
from test_components import MODEL, formalization, needs_lean

CONSERVES = StepContract("transfer_conserves_total", "A transfer never changes the total.", "transfer",
                         "∀ r ∈ result, sumBal r.1.balances = sumBal s.balances", origin="contract")
RULE_LEAN = ("∀ s, Ledger.Reachable s → ∀ a b n s' r, Ledger.op_transfer s a b n = some (s', r) → "
             "Ledger.sumBal s'.balances = Ledger.sumBal s.balances")
PROOF = """\
theorem rule_holds : rule := by
  unfold rule Ledger.spec_transfer_conserves_total
  intro h s hs a b n s' r hop
  have := h s hs a b n
  simp_all [Ledger.post_transfer_conserves_total]
"""


def _ledger(tmp_path: Path, *, verdict: str = "passed", proved: bool = True) -> tuple[Subject, Report]:
    d = formalization()
    spec = ComponentSpec(
        component="Ledger", understanding="", init_params=[], init_pre_english="", init_pre_lean="True",
        operations=[Operation(o["method"], [Param(p["name"], p["lean_type"]) for p in o["params"]], o["returns"]) for o in d["operations"]],
        observers=[Observer("total", "Int", "call")], model_code=MODEL, steps=[CONSERVES],
    )
    run = tmp_path / "ledger-run"
    run.mkdir(parents=True)
    (run / "spec.json").write_text(json.dumps(spec.to_json()))
    status = "proved" if proved else "unproved"
    rep = Report(function="Ledger", file="ledger.py", kind="component", verdict=verdict, artifacts_dir=str(run),
                 drt={"valid": 500, "disagreements": 0},
                 specs=[SpecResult(CONSERVES.name, "postcondition", CONSERVES.english, CONSERVES.lean, "approved",
                                   proof=ProofResult(CONSERVES.name, status, "auto: grind"), origin="contract",
                                   contract=CONSERVES.english)])
    subj = Subject(kind="component", target="ledger.py::Ledger", file=tmp_path / "LARCH.md", line=3, name="Ledger")
    return subj, rep


def _llm(proof: str = PROOF, lean: str = RULE_LEAN):
    from larch.llm.base import LLM, Ledger
    from larch.llm.providers import FakeProvider

    def respond(req):
        if req.stage == "formalize":
            return {"english": "A successful transfer leaves the total balance unchanged, in every reachable state.",
                    "lean": lean, "assumes": ["Larch.Ledger.spec_transfer_conserves_total"], "notes": ""}
        return f"```lean\n{proof}\n```"

    return LLM(FakeProvider(respond), Ledger())


RULE = Contract("Moving store credit between customers never changes how much credit the shop owes.", line=12, uses=["Ledger"])


def _cfg(tmp_path: Path) -> Config:
    return Config(auto_approve=True, proof_attempts=2, artifacts=str(tmp_path / "runs"))


def test_namespacing():
    from larch.engine.system_session import namespace_of, namespaced

    text = "/-! x -/\nset_option linter.unusedVariables false\nnamespace Larch\n\ndef init : Nat := 0\n\nend Larch\n"
    assert namespaced(text, "Cart") == "/-! x -/\nnamespace Larch.Cart\n\ndef init : Nat := 0\n\nend Larch.Cart"
    assert namespace_of(Subject("service", "service orders", Path("L.md"), 1, name="orders")) == "orders"
    qualified = "namespace Larch\ndef bal : Nat := 0\ndef spec_x : Prop := Larch.bal = 0 ∧ Larch.Other.y\nend Larch\n"
    assert "Larch.orders.bal = 0 ∧ Larch.Other.y" in namespaced(qualified, "orders")


@needs_lean
def test_rule_proved_from_component_contract(tmp_path):
    from larch.engine.system_session import verify_rule

    report = verify_rule(RULE, 1, tmp_path / "LARCH.md", [_ledger(tmp_path)], _cfg(tmp_path), llm=_llm())
    assert report.verdict == "passed", (report.error, report.headline, [s.proof for s in report.specs])
    assert [s.name for s in report.specs] == ["rule", "Ledger.spec_transfer_conserves_total"]
    assert report.specs[0].proof.status == "proved" and report.specs[0].contract == RULE.text
    assert "500 tests" in report.headline


@needs_lean
def test_rule_verdict_follows_its_components(tmp_path):
    from larch.engine.system_session import verify_rule

    weak = verify_rule(RULE, 1, tmp_path / "a" / "LARCH.md", [_ledger(tmp_path / "a", proved=False)], _cfg(tmp_path), llm=_llm())
    assert weak.verdict == "partial" and "tested but not proved" in weak.headline
    bug = verify_rule(RULE, 1, tmp_path / "b" / "LARCH.md", [_ledger(tmp_path / "b", verdict="bug")], _cfg(tmp_path), llm=_llm())
    assert bug.verdict == "bug" and "Ledger" in bug.headline


@needs_lean
def test_rule_that_does_not_follow_is_not_proved(tmp_path):
    from larch.engine.system_session import verify_rule

    # "A transfer never changes the source's balance": false, and no contract implies it.
    false_rule = ("∀ s, Ledger.Reachable s → ∀ a b n s' r, Ledger.op_transfer s a b n = some (s', r) → "
                  "Ledger.bal s'.balances a = Ledger.bal s.balances a")
    report = verify_rule(RULE, 1, tmp_path / "LARCH.md", [_ledger(tmp_path)], _cfg(tmp_path),
                         llm=_llm(proof=PROOF, lean=false_rule))
    assert report.verdict == "partial" and report.specs[0].proof.status == "unproved"


@needs_lean
def test_rule_that_does_not_typecheck_is_repaired_or_reported(tmp_path):
    from larch.engine.system_session import verify_rule

    report = verify_rule(RULE, 1, tmp_path / "LARCH.md", [_ledger(tmp_path)], _cfg(tmp_path),
                         llm=_llm(lean="∀ s, Ledger.NoSuchThing s"))
    assert report.verdict == "error" and "could not state the rule" in report.error


def test_cli_routes_rules_to_their_verified_components(tmp_path, monkeypatch):
    from rich.console import Console

    from larch import cli
    from larch.engine import system_session
    from larch.ui import UI

    (tmp_path / ".git").mkdir()
    (tmp_path / "ledger.py").write_text("class Ledger:\n    pass\n")
    md = tmp_path / "LARCH.md"
    md.write_text("# Contracts\n\n## ledger.py::Ledger\n- x\n\n## service orders\nstart: x {port}\n- y\n\n"
                  "# System rules\n- Credit is conserved.  (uses: Ledger)\n- Orders and credit agree.  (uses: Ledger, service orders)\n")
    calls = []

    def fake_verify_rule(rule, number, md_, used, cfg, ui):
        calls.append((number, [s.label for s, _ in used]))
        return Report(function=f"rule {number}", file=str(md_), kind="rule", verdict="passed")

    monkeypatch.setattr(system_session, "verify_rule", fake_verify_rule)
    ledger = Report(function="Ledger", file="ledger.py", kind="component", verdict="passed")
    console = Console(file=open(tmp_path / "out.txt", "w"))
    assert cli._verify_rules(md, {"ledger.py::Ledger": ledger}, {}, {}, UI(), console) is None
    assert calls == [(1, ["ledger.py::Ledger"])]  # rule 2 skipped: the service was not verified
    console.file.close()
    assert "skipping system rule 2" in (tmp_path / "out.txt").read_text()
