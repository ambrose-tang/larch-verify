"""Fast unit tests (no Lean, no LLM)."""
from __future__ import annotations

import random
import textwrap
from pathlib import Path

import pytest

from larch.lean.diagnostics import parse_messages
from larch.lean.lint import lint_lean, strip_comments_and_strings
from larch.lean.types import EncodeError, LType, decode, encode, parse_type, perturb, render
from larch.py.extract import ExtractError, extract, list_functions, splice_function
from larch.py.mutate import generate_mutants
from larch.spec import FormalSpec, Param, Postcondition, harness_module, lean_binder, model_module
from larch.util import extract_code_block


# -- Lean types / codec -----------------------------------------------------------------------

@pytest.mark.parametrize("src,expected", [
    ("Int", "Int"),
    ("List Int", "List Int"),
    ("Option (List (Int × Nat))", "Option (List (Int × Nat))"),
    ("Int × Int × Int", "Int × Int × Int"),
    ("(Int × Int) × Bool", "(Int × Int) × Bool"),
    ("Array String", "Array String"),
])
def test_parse_render_roundtrip(src, expected):
    assert render(parse_type(src)) == expected


def test_parse_rejects_unsupported():
    with pytest.raises(ValueError):
        parse_type("Float")
    with pytest.raises(ValueError):
        parse_type("List")


def test_encode_nested_products_match_lean_json():
    t = parse_type("Int × Int × Int")
    assert encode((1, 2, 3), t) == [1, [2, 3]]
    assert decode([1, [2, 3]], t) == (1, 2, 3)


def test_encode_domain_errors():
    with pytest.raises(EncodeError):
        encode(-1, parse_type("Nat"))
    with pytest.raises(EncodeError):
        encode(None, parse_type("Int"))
    with pytest.raises(EncodeError):
        encode("ab", parse_type("Char"))
    assert encode(None, parse_type("Option Int")) is None
    assert encode(True, parse_type("Int")) == 1


def test_perturb_changes_values():
    rng = random.Random(0)
    for v, t in [(3, "Int"), (True, "Bool"), ([1, 2], "List Int"), (None, "Option Int"), ((1, "a"), "Int × String")]:
        lt = parse_type(t)
        alts = perturb(v, lt, rng)
        assert alts, t
        assert all(a != v for a in alts), t


# -- lint ------------------------------------------------------------------------------------

def test_lint_catches_unsound_constructs():
    bad = """
theorem t : 1 = 2 := by sorry
axiom cheat : False
theorem u : True := by native_decide
@[implemented_by foo] def bar := 1
partial def loop (n : Nat) : Nat := loop n
set_option debug.skipKernelTC true
macro "trivial" : tactic => `(tactic| sorry)
#eval IO.println "hi"
import Mathlib
"""
    toks = {i.token for i in lint_lean(bad)}
    for t in ["sorry", "axiom", "native_decide", "implemented_by", "partial", "set_option debug.skipKernelTC", "macro", "#eval", "import"]:
        assert any(t in x for x in toks), (t, toks)


def test_lint_ignores_comments_and_strings():
    ok = """
-- we do not use sorry here
/- nor admit /- nested sorry -/ -/
def s : String := "sorry, admit, axiom"
theorem t (xs : List Nat) : xs.length = xs.length := by rfl
"""
    assert lint_lean(ok) == []


def test_lint_model_forbids_instances():
    assert lint_lean("instance : LE Int := ⟨fun _ _ => True⟩", model_file=True)
    assert not lint_lean("structure P where\n  x : Int\n  deriving Repr, BEq", model_file=True)


def test_strip_preserves_lines():
    src = "a\n/- x\ny -/\nb -- c\n\"s\"\n"
    assert strip_comments_and_strings(src).count("\n") == src.count("\n")


# -- diagnostics ---------------------------------------------------------------------------------

def test_parse_lean_messages():
    out = textwrap.dedent("""\
        F.lean:3:7: error(lean.synthInstanceFailed): failed to synthesize
          ToJson Char
        F.lean:10:2: warning: declaration uses `sorry`
        """)
    msgs = parse_messages(out)
    assert [(m.line, m.severity) for m in msgs] == [(3, "error"), (10, "warning")]
    assert "ToJson Char" in msgs[0].text


# -- extraction & mutation ------------------------------------------------------------------------

SRC = '''\
import math

LIMIT = 10


def helper(x):
    return x * 2


def target(xs: list[int], k: int = 0) -> int:
    """Sum of doubled elements above k."""
    total = 0
    for x in xs:
        if x > k and x < LIMIT:
            total += helper(x)
    return total


class C:
    @staticmethod
    def s(a: int) -> int:
        return a + 1

    def m(self):
        return 1
'''


def test_extract_context_and_params(tmp_path: Path):
    f = tmp_path / "m.py"
    f.write_text(SRC)
    info = extract(f, "target")
    assert [p.name for p in info.params] == ["xs", "k"]
    assert info.params[1].has_default
    assert "def helper" in info.context and "LIMIT = 10" in info.context and "import math" in info.context
    assert info.docstring.startswith("Sum")
    assert set(list_functions(f)) == {"helper", "target", "C.s"}
    with pytest.raises(ExtractError):
        extract(f, "C.m")
    assert extract(f, "C.s").params[0].name == "a"


def test_splice_function(tmp_path: Path):
    f = tmp_path / "m.py"
    f.write_text(SRC)
    info = extract(f, "target")
    new = splice_function(info, "def target(xs, k=0):\n    return 0\n")
    assert "return 0" in new and "def helper" in new and "class C" in new
    compile(new, "x", "exec")


def test_mutants_are_distinct_and_compile(tmp_path: Path):
    f = tmp_path / "m.py"
    f.write_text(SRC)
    info = extract(f, "target")
    muts = generate_mutants(info, max_mutants=100)
    assert len(muts) >= 8
    srcs = {m.function_source for m in muts}
    assert len(srcs) == len(muts)
    ops = {m.operator for m in muts}
    assert {"ROR", "AOR", "LCR"} <= ops
    for m in muts:
        compile(m.module_source, "x", "exec")
        assert "def helper" in m.module_source  # rest of module preserved
        assert '"""Sum of doubled elements above k."""' in m.module_source or "Sum of doubled" in m.module_source


def test_lean_binder_names():
    assert lean_binder("x") == "x"
    assert lean_binder("from") == "from_"
    assert lean_binder("result") == "result_"
    assert lean_binder("model") == "model_"


# -- spec rendering ---------------------------------------------------------------------------------

def _clamp_spec() -> FormalSpec:
    return FormalSpec(
        function="clamp", understanding="", params=[Param("x", "Int"), Param("lo", "Int"), Param("hi", "Int")],
        return_type="Int", exceptions=False,
        model_code="def model (x lo hi : Int) : Int := if x < lo then lo else if x > hi then hi else x",
        pre_english="lo ≤ hi", pre_lean="lo ≤ hi",
        postconditions=[Postcondition("in_range", "in range", "lo ≤ result ∧ result ≤ hi")],
    )


def test_model_module_shape():
    text = model_module(_clamp_spec())
    assert "namespace Larch" in text and text.strip().endswith("end Larch")
    assert "def spec_in_range : Prop" in text
    assert "∀ (x : Int) (lo : Int) (hi : Int), pre x lo hi → post_in_range x lo hi (model x lo hi)" in text
    assert "example : (Int) → (Int) → (Int) → (Int) := model" in text


def test_harness_avoids_name_clashes():
    spec = _clamp_spec()
    spec.params = [Param("j", "Int"), Param("args", "Int"), Param("hi", "Int")]
    spec.model_code = "def model (j args hi : Int) : Int := j"
    text = harness_module(spec)
    assert "let j : Int ← argAt larchArgs 0" in text
    assert "let args : Int ← argAt larchArgs 1" in text


def test_spec_validation():
    s = _clamp_spec()
    assert s.validate() == []
    s.postconditions.append(Postcondition("Bad-Name", "", "True"))
    assert any("snake_case" in p for p in s.validate())


def test_extract_code_block():
    t = "text\n```lean\ntheorem a : True := trivial\n```\nmore\n```lean\ntheorem b : True := trivial\n```"
    assert extract_code_block(t) == "theorem b : True := trivial"
    assert extract_code_block("no code") is None


# -- harness client robustness -------------------------------------------------------------------

def test_harness_client_skips_non_json_lines(tmp_path: Path):
    import sys

    from larch.lean.harness_client import HarnessClient

    script = tmp_path / "fake_harness.py"
    script.write_text(
        "import sys, json\n"
        "print('Foo.lean:1:1: warning: unused variable', flush=True)\n"
        "for line in sys.stdin:\n"
        "    req = json.loads(line)\n"
        "    if req.get('op') == 'ping':\n"
        "        print(json.dumps({'pong': True}), flush=True)\n"
        "    else:\n"
        "        print('PANIC at something', flush=True)\n"
        "        print(json.dumps({'echo': req}), flush=True)\n"
    )
    c = HarnessClient([sys.executable, str(script)], env={}, cwd=str(tmp_path), timeout=5)
    c.start()
    assert c.request({"op": "case", "x": 1}) == {"echo": {"op": "case", "x": 1}}
    c.close()


def test_exceptions_harness_uses_option():
    spec = _clamp_spec()
    spec.exceptions = True
    assert spec.model_ret == "Option (Int)"
    text = harness_module(spec)
    assert "def encRes (r : Option (Int)) : Json" in text
    assert "set_option linter.all false" in text
    assert "(Option (Int))" in model_module(spec)
