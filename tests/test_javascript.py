"""JavaScript / TypeScript backend: parser, adapter (real Node), and the full pipeline
(real Lean, scripted LLM)."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from larch.config import Config
from larch.engine.impl_client import ImplClient, LoadError
from larch.engine.runner import JobRunner
from larch.js.backend import JS, TS, generate_mutants
from larch.js.parse import JSParseError, Module, tokenize
from larch.lang import ExtractError, language_for

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node not installed")

CLAMP_TS = '''\
import { helper } from "./helper.ts";

const UNUSED = 1;

/**
 * Clamp x into [lo, hi]; requires lo <= hi.
 */
function clamp(x: number, lo: number, hi: number): number {
  console.log("noise", x);
  if (x < lo) {
    return lo;
  }
  if (x > hi) {
    return helper(hi);
  }
  return x;
}

export const other = 1;
'''
HELPER_TS = "export function helper(x: number): number { return x; }\n"

CLAMP_CJS = '''\
/**
 * Clamp x into [lo, hi]; requires lo <= hi.
 * @param {number} x
 * @param {number} lo
 * @param {number} hi
 * @returns {number}
 */
function clamp(x, lo, hi) {
  return Math.max(lo, Math.min(x, hi));
}
module.exports = { other: 1 };
'''


# -- lexer / parser ---------------------------------------------------------------------------------

def test_lexer_skips_braces_in_strings_templates_regex_and_comments():
    src = "const a = '}'; const b = `${'}'} ${ `x${1}` }`; const r = /[}/]+/g; // }\n/* } */ function f() { return 1; }"
    m = Module(src)
    assert [f.name for f in m.functions] == ["f"]
    assert any(t.kind == "regex" for t in tokenize(src))


def test_parser_unbalanced_is_an_error():
    with pytest.raises(JSParseError):
        Module("function f() { return 1;")


def test_parser_declaration_forms():
    src = '''\
export default function a(x: number): number { return x; }
export const b = (xs: Array<number>, n = 2): number => xs.length + n;
const c = function (s: string) { return s; };
const d = x => x * 2;
async function e() {}
function* g() {}
class K {
  static s(p?: number | undefined): string | null { return null; }
  inst(y: number) { return y; }
  private static helper = (z: number) => z;
}
function h({ a }: { a: number }) { return a; }
'''
    m = Module(src)
    by = {f.name: f for f in m.functions}
    assert set(by) >= {"a", "b", "c", "d", "e", "g", "K.s", "K.inst", "h"}
    assert by["b"].params[0].annotation == "Array<number>" and by["b"].params[1].has_default
    assert by["K.s"].params[0].optional and by["K.s"].returns == "string | null"
    assert by["e"].is_async and by["g"].is_generator and not by["K.inst"].is_static
    assert by["h"].params[0].pattern


def test_list_functions_filters_unsupported(tmp_path):
    f = tmp_path / "m.ts"
    f.write_text("export function ok(a: number) { return a; }\nasync function no1() {}\nfunction _private() {}\n"
                 "function rest(...xs: number[]) { return 0; }\n")
    assert TS.list_functions(f) == ["ok"]


def test_extract_ts_and_jsdoc_types(tmp_path):
    (tmp_path / "c.ts").write_text(CLAMP_TS)
    info = TS.extract(tmp_path / "c.ts", "clamp")
    assert info.language == "typescript" and info.returns == "number"
    assert [p.annotation for p in info.params] == ["number"] * 3
    assert info.docstring.startswith("Clamp x") and info.source.lstrip().startswith("/**")
    assert "import { helper }" in info.context and "UNUSED" not in info.context
    (tmp_path / "c.js").write_text(CLAMP_CJS)
    info = JS.extract(tmp_path / "c.js", "clamp")
    assert [p.annotation for p in info.params] == ["number"] * 3 and info.returns == "number"
    with pytest.raises(ExtractError):
        JS.extract(tmp_path / "c.js", "nope")


def test_language_registry():
    assert language_for("a.py").name == "python"
    assert language_for("a.mts").name == "typescript"
    assert language_for("a.cjs").name == "javascript"
    assert language_for("a.rb") is None


def test_mutants_cover_operators_and_skip_types(tmp_path):
    (tmp_path / "c.ts").write_text(CLAMP_TS)
    info = TS.extract(tmp_path / "c.ts", "clamp")
    ms = generate_mutants(info, max_mutants=100)
    ops = {m.operator for m in ms}
    assert {"ROR"} <= ops
    assert all("number" not in m.description for m in ms)
    for m in ms:
        assert m.module_source != info.module_source
        assert "function clamp" in m.function_source


def test_splice_replaces_jsdoc_and_function(tmp_path):
    (tmp_path / "c.ts").write_text(CLAMP_TS)
    info = TS.extract(tmp_path / "c.ts", "clamp")
    new = TS.splice_function(info, "/** doc */\nfunction clamp(x: number, lo: number, hi: number): number {\n  return x;\n}")
    assert "console.log" not in new and "export const other = 1;" in new and new.startswith("import { helper }")


# -- adapter (real Node) ----------------------------------------------------------------------------

def _client(tmp_path: Path, lang, path: Path, func: str):
    info = lang.extract(path, func)
    rt = lang.runtime(info, Config())
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    return info, rt, ImplClient(rt.cmd, rt.env, str(work), log_path=str(work / "log"))


@needs_node
def test_ts_adapter_calls_non_exported_function_and_reloads_source(tmp_path):
    (tmp_path / "package.json").write_text('{"name": "p"}')
    (tmp_path / "c.ts").write_text(CLAMP_TS)
    (tmp_path / "helper.ts").write_text(HELPER_TS)
    info, rt, c = _client(tmp_path, TS, tmp_path / "c.ts", "clamp")
    with c:
        c.load(rt.load)
        assert c.call([5, 0, 3], 2.0) == {"status": "ok", "value": 3}
        assert c.call([-5, 0, 3], 2.0) == {"status": "ok", "value": 0}
        c.load(rt.load, source=CLAMP_TS.replace("return x;", "return x + 100;"))
        assert c.call([1, 0, 3], 2.0) == {"status": "ok", "value": 101}
        with pytest.raises(LoadError):
            c.load(rt.load, source="function clamp(x: number {")
    # Replacement sources are written to the scratch directory, never next to the user's files.
    assert sorted(p.name for p in tmp_path.iterdir() if p.suffix == ".ts") == ["c.ts", "helper.ts"]


@needs_node
def test_cjs_adapter_values_errors_and_timeouts(tmp_path):
    f = tmp_path / "m.cjs"
    f.write_text(
        "function f(x) {\n"
        "  if (x === 1) throw new RangeError('bad');\n"
        "  if (x === 2) { while (true) {} }\n"
        "  if (x === 3) return [1, 'a', null, true, 2 ** 60, 0.5, new Set([1])];\n"
        "  if (x === 4) return 12345678901234567890n;\n"
        "  return x;\n}\nmodule.exports = {};\n"
    )
    info, rt, c = _client(tmp_path, JS, f, "f")
    with c:
        c.load(rt.load)
        assert "RangeError: bad" in c.call([1], 2.0)["exc"]
        assert c.call([2], 0.3) == {"status": "timeout"}
        v = c.call([3], 2.0)["value"]
        assert v[:4] == [1, "a", None, True] and v[4] == 2**60 and v[5] == 0.5 and repr(v[6]).startswith("Set")
        assert c.call([4], 2.0)["value"] == 12345678901234567890
        assert c.call([7], 2.0)["value"] == 7


@needs_node
def test_missing_npm_package_is_explained(tmp_path):
    (tmp_path / "package.json").write_text('{"name": "p"}')
    f = tmp_path / "m.mjs"
    f.write_text("import left from 'left-pad-not-installed';\nexport function f(x) { return x; }\n")
    info = JS.extract(f, "f")
    rt = JS.runtime(info, Config())
    res = JobRunner(rt, tmp_path / "work").check()
    assert not res["ok"] and res["missing"] == "left-pad-not-installed"
    assert "npm ci" in JS.explain_load_error(rt, info, res)


# -- full pipeline (real Lean, scripted LLM) ---------------------------------------------------------

def _lean():
    from larch.lean.toolchain import ToolchainError, find_toolchain

    try:
        return find_toolchain()
    except ToolchainError:
        return None


@needs_node
@pytest.mark.skipif(_lean() is None, reason="pinned Lean toolchain not installed")
@pytest.mark.parametrize("lang_file, src", [("c.ts", CLAMP_TS), ("c.cjs", CLAMP_CJS)])
def test_pipeline_typescript_and_javascript(tmp_path, lang_file, src):
    from test_integration import FORMALIZATION, _cfg

    from larch.engine.session import verify_function
    from larch.llm.base import LLM, Ledger
    from larch.llm.providers import FakeProvider

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "package.json").write_text('{"name": "p"}')
    (proj / "helper.ts").write_text(HELPER_TS)
    good = proj / lang_file
    good.write_text(src)
    correct_function = language_for(good).extract(good, "clamp").source

    def respond(req):
        if req.stage == "formalize":
            return FORMALIZATION
        if req.stage == "adjudicate":
            return {"verdict": "implementation_bug", "explanation": "off by one above the interval"}
        if req.stage == "fix":
            return {"explanation": "restore the upper bound", "fixed_function": correct_function}
        return "```lean\n-- nothing\n```"

    cfg = _cfg(tmp_path)
    cfg.mutants = 12
    report = verify_function(good, "clamp", cfg, llm=LLM(FakeProvider(respond), Ledger()))
    assert report.verdict == "passed", (report.error, report.headline, report.warnings)
    assert report.drt["valid"] > 100 and report.drt["disagreements"] == 0
    assert report.mutation and report.mutation.total > 0 and report.mutation.killed > 0
    assert report.language == language_for(good).name

    buggy = src.replace("return helper(hi);", "return helper(hi) + 1;").replace("Math.min(x, hi)", "Math.min(x, hi + 1)")
    assert buggy != src
    good.write_text(buggy)
    report = verify_function(good, "clamp", cfg, llm=LLM(FakeProvider(respond), Ledger()))
    assert report.verdict == "bug", (report.error, report.headline)
    assert any("in_range" in f.violated_specs for f in report.findings)
    fix = next(f.fix for f in report.findings if f.fix)
    assert fix.validated, fix.validation
