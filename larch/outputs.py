"""Machine-readable outputs for CI: SARIF 2.1.0 (GitHub/GitLab code scanning, most
security dashboards), JUnit XML (every CI test-report view), and a Markdown summary
suitable for a pull-request comment or a job summary ($GITHUB_STEP_SUMMARY)."""
from __future__ import annotations

import json
import os
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

from . import __version__
from .report import Report

RULES = {
    "spec_violation": ("larch/spec-violation", "Implementation violates an approved spec"),
    "divergence": ("larch/model-disagreement", "Implementation disagrees with the verified model"),
    "value": ("larch/model-disagreement", "Implementation disagrees with the verified model"),
    "crash": ("larch/crash", "Implementation raises on a valid input"),
    "timeout": ("larch/timeout", "Implementation does not terminate on a valid input"),
    "type": ("larch/wrong-type", "Implementation returns a value of the wrong type"),
    "doc_example": ("larch/doc-example", "Implementation contradicts a documented example"),
}
_LEVEL = {"confirmed": "error", "likely": "error", "possible": "warning"}


def _rel(path: str, root: Path) -> str:
    try:
        return Path(os.path.relpath(Path(path).resolve(), root)).as_posix()
    except ValueError:
        return Path(path).as_posix()


def to_sarif(reports: list[Report], root: Path) -> dict:
    rules: dict[str, dict] = {}
    results = []
    for r in reports:
        uri = _rel(r.file, root)
        fn = r.function.split(".")[-1]
        for f in r.findings:
            rule_id, title = RULES.get(f.kind, (f"larch/{f.kind}", f.title()))
            rules.setdefault(rule_id, {
                "id": rule_id, "name": title.replace(" ", ""), "shortDescription": {"text": title},
                "helpUri": "https://github.com/ambrose-tang/larch-verify#what-gets-reported",
                "properties": {"tags": ["correctness", "verification"]},
            })
            if r.kind == "function":
                lines = [f"{f.title()} ({f.confidence}): {fn}({f.args_repr}) returned {f.impl}, expected {f.model}."]
            else:
                lines = [f"{f.title()} ({f.confidence}): after `{'; '.join(f.args_repr.splitlines())}` the code gives {f.impl}, "
                         f"the verified model {f.model}."]
            if f.violated_specs:
                lines.append("Violates: " + ", ".join(f.violated_specs) + ".")
            if f.explanation:
                lines.append(f.explanation)
            if f.fix and f.fix.validated:
                lines.append("A fix validated against the verified model is attached to the run's report.")
            results.append({
                "ruleId": rule_id,
                "level": _LEVEL.get(f.confidence, "warning"),
                "message": {"text": " ".join(lines)},
                "locations": [{"physicalLocation": {
                    "artifactLocation": {"uri": uri, "uriBaseId": "%SRCROOT%"},
                    "region": {"startLine": max(1, r.line or 1)},
                }}],
                "partialFingerprints": {"larch/function": f"{uri}::{r.function}:{rule_id}"},
                "properties": {"confidence": f.confidence, "function": r.function, "occurrences": f.count},
            })
        if r.verdict == "error" and r.error:
            rules.setdefault("larch/not-verified", {
                "id": "larch/not-verified", "name": "NotVerified",
                "shortDescription": {"text": "Larch could not verify this function"},
            })
            results.append({
                "ruleId": "larch/not-verified", "level": "note",
                "message": {"text": f"{r.headline} {r.error}"[:2000]},
                "locations": [{"physicalLocation": {"artifactLocation": {"uri": uri, "uriBaseId": "%SRCROOT%"},
                                                    "region": {"startLine": max(1, r.line or 1)}}}],
            })
    return {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [{
            "tool": {"driver": {"name": "Larch", "version": __version__, "semanticVersion": __version__,
                                "informationUri": "https://github.com/ambrose-tang/larch-verify",
                                "rules": list(rules.values())}},
            "originalUriBaseIds": {"%SRCROOT%": {"uri": root.resolve().as_uri() + "/"}},
            "results": results,
        }],
    }


def to_junit(reports: list[Report], root: Path) -> str:
    failures = sum(1 for r in reports if r.verdict == "bug")
    errors = sum(1 for r in reports if r.verdict == "error")
    total_time = sum(r.elapsed_s for r in reports)
    out = [f'<?xml version="1.0" encoding="UTF-8"?>',
           f'<testsuites name="larch" tests="{len(reports)}" failures="{failures}" errors="{errors}" time="{total_time:.1f}">',
           f'  <testsuite name="larch" tests="{len(reports)}" failures="{failures}" errors="{errors}" time="{total_time:.1f}">']
    for r in reports:
        out.append(f'    <testcase classname={quoteattr(_rel(r.file, root))} name={quoteattr(r.function)} time="{r.elapsed_s:.1f}">')
        if r.verdict == "bug":
            f = next((f for f in r.findings if f.confidence in ("confirmed", "likely")), r.findings[0] if r.findings else None)
            msg = r.headline
            body = r.to_markdown()
            out.append(f'      <failure message={quoteattr(msg)} type={quoteattr(f.kind if f else "bug")}>{escape(body)}</failure>')
        elif r.verdict == "error":
            out.append(f'      <error message={quoteattr(r.headline)} type="error">{escape(r.error)}</error>')
        props = [("verdict", r.verdict), ("proved", f"{r.proved}/{len(r.active_specs)}"),
                 ("tested_inputs", str(r.drt.get("valid", 0))), ("cost_usd", f"{r.cost_usd:.3f}")]
        if r.mutation and r.mutation.total:
            props.append(("mutation_score", f"{r.mutation.score:.2f}"))
        out.append("      <properties>" + "".join(f"<property name={quoteattr(k)} value={quoteattr(v)}/>" for k, v in props)
                   + "</properties>")
        if r.verdict == "partial":
            out.append(f"      <system-out>{escape(r.headline)}</system-out>")
        out.append("    </testcase>")
    out += ["  </testsuite>", "</testsuites>"]
    return "\n".join(out) + "\n"


def to_markdown_summary(reports: list[Report], root: Path) -> str:
    if not reports:
        return ("## Larch verification\n\nNo functions to verify (nothing changed, or no changed function has "
                "approved specs in `.larch/specs`).\n")
    icon = {"passed": "✅", "bug": "❌", "partial": "🟡", "error": "⚠️"}
    bugs = [r for r in reports if r.verdict == "bug"]
    head = (f"**Larch: {len(bugs)} bug(s) found in {len(reports)} function(s)**" if bugs
            else f"**Larch: no bugs found in {len(reports)} function(s)**")
    L = ["## Larch verification", "", head, "",
         "| | function | specs proved | inputs tested | mutation | result |", "|---|---|---|---|---|---|"]
    for r in reports:
        m = r.mutation
        L.append(f"| {icon.get(r.verdict, '')} | `{_rel(r.file, root)}::{r.function}` | {r.proved}/{len(r.active_specs)} | "
                 f"{r.drt.get('valid', 0):,} | {f'{m.score:.0%}' if m and m.total else '–'} | {r.headline.replace('|', '/')} |")
    for r in bugs:
        L += ["", f"### ❌ `{r.function}` ({_rel(r.file, root)}:{r.line})"]
        for f in r.findings:
            if f.confidence not in ("confirmed", "likely"):
                continue
            if r.kind != "function":
                L += [f"- **{f.title()}** ({f.confidence}): the code gives `{f.impl}`, the verified model `{f.model}`, after:",
                      "", "```python", f.args_repr, "```"]
            else:
                L += [f"- **{f.title()}** ({f.confidence}): `{r.function.split('.')[-1]}({f.args_repr})` returned `{f.impl}`, "
                      f"expected `{f.model}`" + (f"; violates {', '.join(f'`{s}`' for s in f.violated_specs)}" if f.violated_specs else "")]
            if f.fix and f.fix.diff:
                status = "validated against the verified model" if f.fix.validated else "not validated"
                L += ["", f"<details><summary>Proposed fix ({status})</summary>", "", "```diff", f.fix.diff.rstrip(), "```",
                      "", "</details>"]
    cost = sum(r.cost_usd for r in reports)
    L += ["", f"<sub>Larch {__version__} · ${cost:.2f} · specs are proved about a Lean model of each function; the "
              "implementation is differentially tested against it.</sub>"]
    return "\n".join(L) + "\n"


def write_outputs(reports: list[Report], root: Path, *, sarif: str | None = None, junit: str | None = None,
                  markdown: str | None = None) -> None:
    if sarif:
        Path(sarif).write_text(json.dumps(to_sarif(reports, root), indent=2))
    if junit:
        Path(junit).write_text(to_junit(reports, root))
    if markdown:
        text = to_markdown_summary(reports, root)
        if markdown == "-":
            print(text)
        else:
            # GitHub's job summary file is shared by all steps: append to it.
            append = os.environ.get("GITHUB_STEP_SUMMARY") and os.path.abspath(markdown) == os.path.abspath(os.environ["GITHUB_STEP_SUMMARY"])
            with open(markdown, "a" if append else "w") as fh:
                fh.write(text)
