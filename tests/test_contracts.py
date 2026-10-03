"""LARCH.md contracts and exhaustive testing."""
from __future__ import annotations

from pathlib import Path

import pytest

from larch import contracts as larchmd
from larch.lean.types import domain_size, enumerate_domain, finite_values, parse_type
from larch.spec import FormalSpec, Param, Postcondition, domain_property

LARCH_MD = '''\
# Project contracts

Prose with a bullet that is not a contract:
- just documentation

# Contracts

## pkg/fees.py::fee
- The fee is never negative.
- VIP customers never pay more than others
  for the same amount.

```lean
∀ n, 0 ≤ n → model n true ≤ model n false
```

## pkg/fees.py::Account

## service orders
start: uvicorn app:app --port {port}
reset: POST /test/reset
- GET /orders/{id} is 404 for an unknown id.

include: more.md

# System rules
- Nobody is charged twice.  (uses: pkg/fees.py::Account, service orders)
'''


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "fees.py").write_text(
        "class Account:\n    pass\n\n\ndef fee(n: int, vip: bool) -> int:\n    return n\n"
    )
    (tmp_path / "LARCH.md").write_text(LARCH_MD)
    (tmp_path / "more.md").write_text("## pkg/fees.py::other\n- Returns 0.\n")
    return tmp_path


def test_parse(project):
    cf = larchmd.load(project / "LARCH.md")
    kinds = {s.target: s.kind for s in cf.subjects}
    assert kinds == {"pkg/fees.py::fee": "function", "pkg/fees.py::Account": "component",
                     "service orders": "service", "pkg/fees.py::other": "function"}
    fee = cf.subject("pkg/fees.py::fee")
    assert [c.text for c in fee.contracts] == [
        "The fee is never negative.", "VIP customers never pay more than others for the same amount."]
    assert fee.contracts[1].lean.startswith("∀ n") and fee.contracts[0].lean is None
    svc = cf.subject("service orders")
    assert svc.settings == {"start": "uvicorn app:app --port {port}", "reset": "POST /test/reset"}
    assert cf.rules[0].text == "Nobody is charged twice." and cf.rules[0].uses == ["pkg/fees.py::Account", "service orders"]
    assert cf.for_function(project / "pkg" / "fees.py", "fee") is fee
    assert larchmd.find(project / "pkg") == (project / "LARCH.md").resolve()


def test_parse_errors(tmp_path):
    (tmp_path / "LARCH.md").write_text("# Contracts\n\n## not a target\n- x\n")
    with pytest.raises(larchmd.ContractsError):
        larchmd.load(tmp_path / "LARCH.md")
    (tmp_path / "LARCH.md").write_text("```lean\nTrue\n```\n")
    with pytest.raises(larchmd.ContractsError):
        larchmd.load(tmp_path / "LARCH.md")


def test_write_back_only_fills_empty_headings(project):
    cf = larchmd.load(project / "LARCH.md")
    acct = cf.subject("pkg/fees.py::Account")
    assert larchmd.write_back(acct, ["The balance never goes negative."])
    again = larchmd.load(project / "LARCH.md")
    assert [c.text for c in again.subject("pkg/fees.py::Account").contracts] == ["The balance never goes negative."]
    assert [c.text for c in again.subject("pkg/fees.py::fee").contracts][0] == "The fee is never negative."
    assert not larchmd.write_back(again.subject("pkg/fees.py::fee"), ["something else"])


def test_draft_template():
    text = larchmd.draft(Path("."), ["a.py::f", "b.ts::g"])
    assert "## a.py::f" in text and "# System rules" in text


# -- finite domains ------------------------------------------------------------------------------

def test_finite_values():
    assert finite_values(parse_type("Bool")) == [False, True]
    assert finite_values(parse_type("Option Bool")) == [None, False, True]
    assert finite_values(parse_type("Nat"), (-3, 2)) == [0, 1, 2]
    assert finite_values(parse_type("Int")) is None
    assert finite_values(parse_type("Bool × Bool")) == [(False, False), (False, True), (True, False), (True, True)]
    assert finite_values(parse_type("List Bool")) is None


def test_enumerate_domain_smallest_first():
    types = [parse_type("Int"), parse_type("Bool")]
    dom = enumerate_domain(types, [[-2, 2], None], limit=100)
    assert len(dom) == domain_size(types, [[-2, 2], None]) == 10
    assert dom[0] == [0, False] and set(map(tuple, dom)) == {(i, b) for i in range(-2, 3) for b in (False, True)}
    assert enumerate_domain(types, [[0, 10**6], None], limit=1000) is None


def test_domain_property_statement():
    spec = FormalSpec(function="f", understanding="", params=[Param("n", "Nat"), Param("k", "Option Int"), Param("b", "Bool")],
                      return_type="Int", exceptions=False, model_code="def model (n : Nat) (k : Option Int) (b : Bool) : Int := 0",
                      pre_english="", pre_lean="n ≤ 9", postconditions=[Postcondition("p", "p", "True")],
                      input_bounds={"n": [0, 9], "k": [-1, 1]})
    prop = domain_property(spec)
    assert prop.lean == "pre n k b → ((0 : Nat) ≤ n ∧ n ≤ (9 : Nat)) ∧ (∀ v ∈ k, ((-1 : Int) ≤ v ∧ v ≤ (1 : Int)))"
    assert prop.origin == "larch"


# -- integration: contracts, fidelity checks, exhaustive testing (real Lean, scripted LLM) ---------

def _lean():
    from larch.lean.toolchain import ToolchainError, find_toolchain

    try:
        return find_toolchain()
    except ToolchainError:
        return None


needs_lean = pytest.mark.skipif(_lean() is None, reason="pinned Lean toolchain not installed")

FEE_OK = '''\
def fee(n: int, vip: bool) -> int:
    """Fee in cents for an order of n items. Requires 0 <= n <= 40.
    Standard: 3 per item plus 5. VIP: 3 per item, no flat part."""
    return 3 * n + (0 if vip else 5)
'''
FEE_BUG = FEE_OK.replace("return 3 * n + (0 if vip else 5)", "return 3 * n + (0 if vip else 5) - (4 if n == 37 and vip else 0)")


def _formalization(*, wrong_lean: bool = False, bounds: bool = True) -> dict:
    return {
        "understanding": "3 per item, plus a flat 5 for non-VIP customers.",
        "params": [{"name": "n", "lean_type": "Int"}, {"name": "vip", "lean_type": "Bool"}],
        "return_type": "Int",
        "exceptions": False,
        "model": "def model (n : Int) (vip : Bool) : Int :=\n  3 * n + (if vip then 0 else 5)",
        "precondition": {"english": "0 ≤ n ≤ 40", "lean": "0 ≤ n ∧ n ≤ 40"},
        "postconditions": [
            {"name": "nonneg", "english": "The fee is at least zero.",
             "lean": "result ≥ -100" if wrong_lean else "0 ≤ result", "contract": 0},
            {"name": "per_item", "english": "The fee is at least 3 per item.", "lean": "3 * n ≤ result", "contract": 1},
        ],
        "properties": [],
        "input_generator": "def strategy(st):\n    return st.tuples(st.integers(0, 40), st.booleans())",
        "edge_cases": "[[0, false], [40, true]]",
        "notes": "",
        "input_bounds": [{"param": "n", "lo": 0, "hi": 40}] if bounds else [],
        "contract_checks": [
            {"contract": 0, "args": "[1, false]", "output": "-3", "expect": "violates"},
            {"contract": 0, "args": "[1, false]", "output": "8", "expect": "satisfies"},
            {"contract": 1, "args": "[2, true]", "output": "5", "expect": "violates"},
        ],
    }


def _subject(tmp_path: Path, src: str):
    (tmp_path / ".git").mkdir(exist_ok=True)
    f = tmp_path / "fees.py"
    f.write_text(src)
    (tmp_path / "LARCH.md").write_text("# Contracts\n\n## fees.py::fee\n- The fee is never negative.\n"
                                       "- The fee is at least 3 per item.\n")
    return f, larchmd.load(tmp_path / "LARCH.md").subjects[0]


def _llm(responses: list[dict]):
    from larch.llm.base import LLM, Ledger
    from larch.llm.providers import FakeProvider

    seq = iter(responses)
    last = {}

    def respond(req):
        if req.stage == "formalize":
            last["f"] = next(seq, last.get("f"))
            return last["f"]
        if req.stage == "adjudicate":
            return {"verdict": "implementation_bug", "explanation": "VIP fee wrong at 37"}
        if req.stage == "fix":
            return {"explanation": "remove special case", "fixed_function": FEE_OK}
        return "```lean\n-- nothing\n```"

    return LLM(FakeProvider(respond), Ledger())


def _cfg(tmp_path: Path):
    from larch.config import Config

    return Config(auto_approve=True, tests=300, mutants=10, mutation_tests=100, artifacts=str(tmp_path / "runs"))


@needs_lean
def test_exhaustive_pass_with_contracts(tmp_path):
    from larch.engine.session import verify_function

    f, subject = _subject(tmp_path, FEE_OK)
    llm = _llm([_formalization()])
    report = verify_function(f, "fee", _cfg(tmp_path), llm=llm, subject=subject)
    assert report.verdict == "passed", (report.error, report.headline, report.warnings)
    assert report.drt["exhaustive"] and report.drt["complete"] and report.drt["valid"] == 82
    origins = {s.name: (s.origin, s.contract) for s in report.specs}
    assert origins["nonneg"] == ("contract", "The fee is never negative.")
    assert origins["input_domain"][0] == "larch"
    assert "every one of the 82 valid inputs" in report.headline


@needs_lean
def test_exhaustive_finds_single_input_bug(tmp_path):
    from larch.engine.session import verify_function

    f, subject = _subject(tmp_path, FEE_BUG)
    cfg = _cfg(tmp_path)
    cfg.run_mutation = False
    report = verify_function(f, "fee", cfg, llm=_llm([_formalization()]), subject=subject)
    assert report.verdict == "bug", report.headline
    assert report.drt["exhaustive"] and report.drt["disagreements"] == 1  # exactly one of 82 inputs
    assert any(fi.args_repr == "37, True" for fi in report.findings)


@needs_lean
def test_contract_fidelity_check_forces_repair(tmp_path):
    from larch.engine.session import verify_function

    f, subject = _subject(tmp_path, FEE_OK)
    llm = _llm([_formalization(wrong_lean=True), _formalization()])
    cfg = _cfg(tmp_path)
    cfg.run_mutation = False
    report = verify_function(f, "fee", cfg, llm=llm, subject=subject)
    assert report.verdict == "passed", (report.error, report.headline)
    calls = [r for r in llm.provider.requests if r.stage == "formalize"]
    assert len(calls) == 2 and "accepts the result -3" in calls[1].prompt


@needs_lean
def test_uncovered_contract_is_a_problem(tmp_path):
    from larch.engine.session import verify_function

    f, subject = _subject(tmp_path, FEE_OK)
    partial = _formalization()
    partial["postconditions"] = partial["postconditions"][:1]
    partial["contract_checks"] = partial["contract_checks"][:2]
    llm = _llm([partial, _formalization()])
    cfg = _cfg(tmp_path)
    cfg.run_mutation = False
    verify_function(f, "fee", cfg, llm=llm, subject=subject)
    calls = [r for r in llm.provider.requests if r.stage == "formalize"]
    assert len(calls) == 2 and "was not formalized" in calls[1].prompt
    assert "The developer's contracts" in calls[0].prompt


@needs_lean
def test_random_testing_without_bounds(tmp_path):
    from larch.engine.session import verify_function

    f, subject = _subject(tmp_path, FEE_OK)
    cfg = _cfg(tmp_path)
    cfg.run_mutation = False
    report = verify_function(f, "fee", cfg, llm=_llm([_formalization(bounds=False)]), subject=subject)
    assert not report.drt["exhaustive"] and report.drt["valid"] > 0
    assert not any(s.name == "input_domain" for s in report.specs)


@needs_lean
def test_implementation_never_runs_outside_precondition(tmp_path):
    """Inputs that fail the precondition are not part of the contract: the code under
    test must not be called on them (it may hang, allocate, or have side effects)."""
    from larch.engine.session import verify_function

    guarded = FEE_OK.replace('    return 3 * n', '    if n < 0 or n > 40:\n        while True:\n            pass\n    return 3 * n')
    f, subject = _subject(tmp_path, guarded)
    cfg = _cfg(tmp_path)
    cfg.run_mutation = False
    cfg.call_timeout = 0.5
    report = verify_function(f, "fee", cfg, llm=_llm([_formalization(bounds=False)]), subject=subject)
    counts = report.drt["counts"]
    assert counts.get("pre_false", 0) > 0 and counts.get("timeout", 0) == 0, counts
    assert report.verdict != "bug", report.headline


def test_the_repos_own_larch_md_is_valid():
    """Larch is verified by Larch: LARCH.md at the repository root must parse, and every
    function it states contracts for must still exist and be extractable."""
    from pathlib import Path

    from larch import contracts
    from larch.lang import language_for

    root = Path(__file__).resolve().parents[1]
    cf = contracts.load(root / "LARCH.md")
    assert cf.subjects, "LARCH.md names no subjects"
    for s in cf.subjects:
        assert s.contracts, f"{s.target} has no contracts"
        extract = language_for(s.path).extract_class if s.kind == "component" else language_for(s.path).extract
        assert extract(s.path, s.name).name == s.name
