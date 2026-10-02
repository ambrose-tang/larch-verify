"""Repository scanning, change detection, CI outputs, enterprise providers, config discovery."""
from __future__ import annotations

import json
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from larch.cli import main
from larch.config import Config
from larch.outputs import to_junit, to_markdown_summary, to_sarif
from larch.repo import changed_functions, scan
from larch.report import Finding, FixProposal, MutationSummary, Report

PY = '''\
import os


def good(xs: list[int], k: int) -> int:
    """Count the elements of xs that are at least k, ignoring negatives."""
    n = 0
    for x in xs:
        if x >= 0 and x >= k:
            n += 1
    return n


def uses_float(x: float) -> float:
    return x * 2


def reads_env(name: str) -> str:
    """The value of an environment variable (requests are not made here)."""
    return os.environ.get(name, "")


class K:
    def method(self, x: int) -> int:
        return x

    @staticmethod
    def static(x: int) -> int:
        return -x


def _private(x: int) -> int:
    return x
'''

TS = '''\
/** Sum of positive entries. */
export function sumPos(xs: number[]): number {
  let s = 0;
  for (const x of xs) if (x > 0) s += x;
  return s;
}
export function when(d: Date): number { return d.getTime(); }
export async function later(x: number): Promise<number> { return x; }
'''


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "m.py").write_text(PY)
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "u.ts").write_text(TS)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_m.py").write_text("def test_x(a: int) -> int:\n    return a\n")
    (tmp_path / "node_modules" / "dep").mkdir(parents=True)
    (tmp_path / "node_modules" / "dep" / "i.js").write_text("function f(a) { return a; }\n")
    (tmp_path / ".gitignore").write_text("ignored/\n")
    (tmp_path / "ignored").mkdir()
    (tmp_path / "ignored" / "x.py").write_text("def f(a: int) -> int:\n    return a\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path


def test_scan_classifies_and_ranks(repo):
    cands = {c.target: c for c in scan([repo])}
    assert cands["src/m.py::good"].status == "ready"
    assert cands["src/m.py::K.static"].status == "ready"
    assert cands["web/u.ts::sumPos"].status == "ready" and cands["web/u.ts::sumPos"].language == "typescript"
    assert "floats" in cands["src/m.py::uses_float"].reason
    assert "impure" in cands["src/m.py::reads_env"].reason  # code, not the docstring, is inspected
    assert "dates" in cands["web/u.ts::when"].reason
    assert "async" in cands["web/u.ts::later"].reason
    assert not any("K.method" in t or "_private" in t for t in cands)
    assert not any(t.startswith(("tests/", "node_modules/", "ignored/")) for t in cands)
    ready = [c for c in scan([repo]) if c.status == "ready"]
    assert ready[0].target == "src/m.py::good"  # documented and branchy ranks first


def test_scan_include_tests(repo):
    assert any(c.file.startswith("tests/") for c in scan([repo], include_tests=True))


def test_changed_functions(repo):
    src = (repo / "src" / "m.py").read_text().replace("return -x", "return -x + 0")
    (repo / "src" / "m.py").write_text(src)
    (repo / "web" / "new.ts").write_text("export function fresh(a: number): number { return a + 1; }\n")
    changed = {c.target for c in changed_functions(scan([repo]), repo, "HEAD")}
    assert changed == {"src/m.py::K.static", "src/m.py::K", "web/new.ts::fresh"}  # the class changed too
    with pytest.raises(ValueError):
        changed_functions(scan([repo]), repo, "no-such-ref")


def test_cli_scan_json(repo, capsys):
    out = repo / "scan.json"
    assert main(["scan", str(repo), "--json", str(out)]) == 0
    data = json.loads(out.read_text())
    assert any(c["target"] == "src/m.py::good" and c["status"] == "ready" for c in data["candidates"])


def test_cli_verify_changed_with_nothing_changed_is_success(repo, monkeypatch):
    monkeypatch.chdir(repo)
    assert main(["verify", "--changed", "HEAD", "--quiet", "--yes"]) == 0


# -- outputs -------------------------------------------------------------------------------------

def _reports(root: Path) -> list[Report]:
    bug = Report(function="clamp", file=str(root / "src" / "c.py"), line=12, verdict="bug",
                 headline="Implementation violates an approved spec: clamp(5, 0, 3) returned 4, expected 3.")
    bug.findings = [Finding("spec_violation", "confirmed", "5, 0, 3", "4", "3", ["in_range"],
                            fix=FixProposal("restore", "--- a\n+++ b\n-x\n+y\n", True))]
    bug.drt = {"valid": 1200}
    bug.mutation = MutationSummary(total=10, killed=9)
    ok = Report(function="f", file=str(root / "web" / "u.ts"), line=1, verdict="passed", headline="All specs proved.")
    err = Report(function="g", file=str(root / "web" / "u.ts"), line=5, verdict="error", headline="Could not run.",
                 error="imports `x`, which is not installed")
    return [bug, ok, err]


def test_sarif(tmp_path):
    s = to_sarif(_reports(tmp_path), tmp_path)
    assert s["version"] == "2.1.0"
    res = s["runs"][0]["results"]
    assert res[0]["ruleId"] == "larch/spec-violation" and res[0]["level"] == "error"
    loc = res[0]["locations"][0]["physicalLocation"]
    assert loc["artifactLocation"]["uri"] == "src/c.py" and loc["region"]["startLine"] == 12
    assert {r["id"] for r in s["runs"][0]["tool"]["driver"]["rules"]} >= {"larch/spec-violation", "larch/not-verified"}


def test_junit(tmp_path):
    root = ET.fromstring(to_junit(_reports(tmp_path), tmp_path))
    suite = root.find("testsuite")
    assert suite.get("tests") == "3" and suite.get("failures") == "1" and suite.get("errors") == "1"
    assert suite.findall("testcase")[0].find("failure") is not None


def test_markdown_summary(tmp_path):
    md = to_markdown_summary(_reports(tmp_path), tmp_path)
    assert "1 bug(s) found in 3 function(s)" in md and "```diff" in md and "`src/c.py::clamp`" in md
    assert "No functions to verify" in to_markdown_summary([], tmp_path)


# -- providers -----------------------------------------------------------------------------------

def test_bedrock_and_vertex_selection(monkeypatch):
    from larch.llm import providers
    from larch.llm.base import LLMError
    from larch.llm.pricing import price_for

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    monkeypatch.delenv("LARCH_AWS_REGION", raising=False)
    with pytest.raises(LLMError, match="region"):
        providers.make_provider("auto")
    p = providers.AnthropicProvider.__new__(providers.AnthropicProvider)
    p.platform = "bedrock"
    assert p.platform_model("claude-sonnet-5") == "anthropic.claude-sonnet-5"
    assert p.platform_model("us.anthropic.claude-sonnet-5") == "us.anthropic.claude-sonnet-5"
    assert price_for("us.anthropic.claude-haiku-4-5") == price_for("claude-haiku-4-5")
    monkeypatch.delenv("CLAUDE_CODE_USE_BEDROCK")
    monkeypatch.setenv("CLAUDE_CODE_USE_VERTEX", "1")
    monkeypatch.delenv("ANTHROPIC_VERTEX_PROJECT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    with pytest.raises(LLMError, match="project"):
        providers.make_provider("auto")


# -- configuration -------------------------------------------------------------------------------

def test_config_found_at_monorepo_root(tmp_path, monkeypatch):
    monkeypatch.delenv("LARCH_TESTS", raising=False)
    (tmp_path / ".git").mkdir()
    (tmp_path / "pyproject.toml").write_text("[tool.larch]\ntests = 123\npythonpath = ['libs']\n")
    pkg = tmp_path / "services" / "billing"
    pkg.mkdir(parents=True)
    (pkg / "package.json").write_text("{}")
    (pkg / ".larch.toml").write_text("mutants = 7\n")
    cfg = Config.load(pkg)
    assert cfg.tests == 123 and cfg.mutants == 7 and cfg.pythonpath == ["libs"]
