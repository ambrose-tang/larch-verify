"""Stateful components (classes): state-machine models, sequence testing, proofs."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from larch.component import ComponentSpec, Invariant, Observer, Operation, StepContract, module_text
from larch.config import Config
from larch.spec import Param

LEDGER_OK = '''\
class InsufficientFunds(Exception):
    pass


class Ledger:
    """Store credit in cents. Balances never go negative; failed operations change nothing."""

    def __init__(self):
        self._b = {}

    def deposit(self, account: str, amount: int) -> None:
        if amount <= 0:
            raise ValueError("amount")
        self._b[account] = self._b.get(account, 0) + amount

    def withdraw(self, account: str, amount: int) -> None:
        if amount <= 0:
            raise ValueError("amount")
        if self._b.get(account, 0) < amount:
            raise InsufficientFunds(account)
        self._b[account] -= amount

    def transfer(self, source: str, target: str, amount: int) -> None:
        self.withdraw(source, amount)
        self._b[target] = self._b.get(target, 0) + amount

    def balance(self, account: str) -> int:
        return self._b.get(account, 0)

    def total(self) -> int:
        return sum(self._b.values())
'''
LEDGER_BUG = LEDGER_OK.replace(
    "        self.withdraw(source, amount)\n        self._b[target] = self._b.get(target, 0) + amount",
    "        if amount <= 0:\n            raise ValueError(\"amount\")\n"
    "        self._b[target] = self._b.get(target, 0) + amount\n        self.withdraw(source, amount)",
)

MODEL = '''\
structure State where
  balances : List (String × Int)
  deriving Repr, DecidableEq

def bal (bs : List (String × Int)) (a : String) : Int :=
  match bs with
  | [] => 0
  | (k, v) :: rest => if k = a then v else bal rest a

def setBal (bs : List (String × Int)) (a : String) (v : Int) : List (String × Int) :=
  (a, v) :: bs.filter (fun p => p.1 ≠ a)

def sumBal (bs : List (String × Int)) : Int := bs.foldr (fun p acc => p.2 + acc) 0

def init : Option State := some ⟨[]⟩

def op_deposit (s : State) (account : String) (amount : Int) : Option (State × Unit) :=
  if amount ≤ 0 then none else some (⟨setBal s.balances account (bal s.balances account + amount)⟩, ())

def op_withdraw (s : State) (account : String) (amount : Int) : Option (State × Unit) :=
  if amount ≤ 0 ∨ bal s.balances account < amount then none
  else some (⟨setBal s.balances account (bal s.balances account - amount)⟩, ())

def op_transfer (s : State) (source target : String) (amount : Int) : Option (State × Unit) :=
  match op_withdraw s source amount with
  | none => none
  | some (s1, _) => some (⟨setBal s1.balances target (bal s1.balances target + amount)⟩, ())

def op_balance (s : State) (account : String) : Option (State × Int) := some (s, bal s.balances account)

def op_total (s : State) : Option (State × Int) := some (s, sumBal s.balances)

def obs_total (s : State) : Int := sumBal s.balances
'''


def formalization() -> dict:
    p = lambda n, t: {"name": n, "lean_type": t}  # noqa: E731
    return {
        "understanding": "Balances per account; failed operations change nothing.",
        "constructor": {"params": [], "precondition": {"english": "none", "lean": "True"}},
        "operations": [
            {"method": "deposit", "params": [p("account", "String"), p("amount", "Int")], "returns": "Unit"},
            {"method": "withdraw", "params": [p("account", "String"), p("amount", "Int")], "returns": "Unit"},
            {"method": "transfer", "params": [p("source", "String"), p("target", "String"), p("amount", "Int")], "returns": "Unit"},
            {"method": "balance", "params": [p("account", "String")], "returns": "Int"},
            {"method": "total", "params": [], "returns": "Int"},
        ],
        "observers": [{"name": "total", "lean_type": "Int", "access": "call"}],
        "model": MODEL,
        "invariants": [],
        "operation_contracts": [
            {"name": "withdraw_short_fails", "english": "Withdrawing more than the balance fails.", "operation": "withdraw",
             "lean": "0 < amount → bal s.balances account < amount → result = none", "contract": 0},
            {"name": "transfer_conserves_total", "english": "A transfer never changes the total.", "operation": "transfer",
             "lean": "∀ r ∈ result, sumBal r.1.balances = sumBal s.balances", "contract": 1},
        ],
        "input_generator": (
            "def strategy(st):\n"
            "    acct = st.sampled_from(['a', 'b'])\n"
            "    amt = st.integers(-1, 6)\n"
            "    return {'init': st.just(()), 'deposit': st.tuples(acct, amt), 'withdraw': st.tuples(acct, amt),\n"
            "            'transfer': st.tuples(acct, acct, amt), 'balance': st.tuples(acct), 'total': st.just(())}"
        ),
        "exhaustive_domains": json.dumps({
            "init": [[]], "deposit": [["a", 3], ["b", 1]], "withdraw": [["a", 2]],
            "transfer": [["a", "b", 2], ["b", "a", 5]], "balance": [["b"]], "total": [[]],
        }),
        "notes": "",
    }


def _lean():
    from larch.lean.toolchain import ToolchainError, find_toolchain

    try:
        return find_toolchain()
    except ToolchainError:
        return None


needs_lean = pytest.mark.skipif(_lean() is None, reason="pinned Lean toolchain not installed")


def _llm(fix: str | None = None):
    from larch.llm.base import LLM, Ledger
    from larch.llm.providers import FakeProvider

    def respond(req):
        if req.stage == "formalize":
            return formalization()
        if req.stage == "adjudicate":
            return {"verdict": "implementation_bug", "explanation": "the failed transfer credited the target"}
        if req.stage == "fix":
            return {"explanation": "debit first", "fixed_function": fix or ""}
        return "```lean\n-- nothing\n```"

    return LLM(FakeProvider(respond), Ledger())


def _cfg(tmp_path: Path) -> Config:
    return Config(auto_approve=True, sequences=200, mutants=8, artifacts=str(tmp_path / "runs"))


def _subject(tmp_path: Path, src: str):
    from larch import contracts

    (tmp_path / ".git").mkdir(parents=True, exist_ok=True)
    f = tmp_path / "ledger.py"
    f.write_text(src)
    (tmp_path / "LARCH.md").write_text("# Contracts\n\n## ledger.py::Ledger\n- Withdrawing more than the balance fails.\n"
                                       "- A transfer never changes the total.\n")
    return f, contracts.load(tmp_path / "LARCH.md").subjects[0]


def test_module_text_shapes():
    d = formalization()
    spec = ComponentSpec(
        component="Ledger", understanding="", init_params=[], init_pre_english="", init_pre_lean="True",
        operations=[Operation(o["method"], [Param(p["name"], p["lean_type"]) for p in o["params"]], o["returns"]) for o in d["operations"]],
        observers=[Observer("total", "Int", "call")], model_code=MODEL,
        invariants=[Invariant("nonneg", "never negative", "∀ p ∈ s.balances, 0 ≤ p.2")],
        steps=[StepContract("t", "t", "transfer", "True")],
    )
    assert spec.validate() == []
    text = module_text(spec)
    assert "inductive Reachable : State → Prop where" in text
    assert "| op_transfer (s : State) (source : String) (target : String) (amount : Int) (s' : State) (r : Unit) :" in text
    assert "def spec_nonneg : Prop :=\n  ∀ s, Reachable s → inv_nonneg s" in text
    assert "∀ s, Reachable s → ∀ (source : String) (target : String) (amount : Int), post_t s source target amount (op_transfer s source target amount)" in text
    bad = ComponentSpec.from_json(spec.to_json())
    bad.steps = [StepContract("x", "x", "nope", "True")]
    assert any("not one of the operations" in p for p in bad.validate())


@needs_lean
def test_correct_ledger_agrees_and_contracts_prove(tmp_path):
    from larch.engine.component_session import verify_component

    f, subject = _subject(tmp_path, LEDGER_OK)
    report = verify_component(f, "Ledger", _cfg(tmp_path), llm=_llm(), subject=subject)
    # The scripted LLM writes no proofs: `withdraw_short_fails` falls to automation; the
    # total-conservation contract needs a helper lemma that only a real prover would supply.
    assert report.verdict in ("passed", "partial"), (report.error, report.headline, report.warnings)
    assert report.drt["disagreements"] == 0 and report.drt["valid"] > 100
    assert report.drt["exhaustive_depth"] and report.drt["exhaustive_cases"] > 100
    assert {s.origin for s in report.specs} == {"contract"}
    assert next(s for s in report.specs if s.name == "withdraw_short_fails").proof.status == "proved"
    assert report.mutation and report.mutation.total > 0


@needs_lean
def test_non_atomic_transfer_found_and_fix_validated(tmp_path):
    from larch.engine.component_session import verify_component
    from larch.lang import language_for

    correct_class = language_for("x.py").extract_class(_subject(tmp_path / "ok", LEDGER_OK)[0], "Ledger").source
    f, subject = _subject(tmp_path, LEDGER_BUG)
    cfg = _cfg(tmp_path)
    cfg.run_mutation = False
    report = verify_component(f, "Ledger", cfg, llm=_llm(fix=correct_class), subject=subject)
    assert report.verdict == "bug", (report.error, report.headline)
    f0 = report.findings[0]
    assert "transfer" in f0.args_repr and f0.args_repr.count("\n") <= 4  # shrunk to a short sequence
    assert f0.fix and f0.fix.validated, f0.fix and f0.fix.validation


@needs_lean
@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_typescript_class_adapter(tmp_path):
    from larch.engine.impl_client import ImplClient
    from larch.lang import language_for

    f = tmp_path / "counter.ts"
    f.write_text("export class Counter {\n  private n = 0;\n  constructor(private step: number) {}\n"
                 "  bump(): number { this.n += this.step; return this.n; }\n  get value(): number { return this.n; }\n}\n")
    lang = language_for(f)
    info = lang.extract_class(f, "Counter")
    assert [m.name for m in info.methods] == ["bump", "value"] and info.methods[1].kind == "property"
    rt = lang.runtime(info, Config())
    work = tmp_path / "w"
    work.mkdir()
    with ImplClient(rt.cmd, rt.env, str(work), log_path=str(work / "log")) as c:
        c.load(rt.load)
        assert c.new([3])["status"] == "ok"
        assert c.invoke("bump", [], 1.0)["value"] == 3
        assert c.observe([{"name": "value", "access": "attribute"}], 1.0)["observers"]["value"]["value"] == 3
