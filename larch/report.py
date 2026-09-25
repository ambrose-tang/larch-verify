"""Result data model for one verification run, plus JSON and Markdown rendering."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class ProofResult:
    name: str
    status: str = "not_attempted"  # proved | unproved | not_attempted
    method: str = ""  # e.g. "auto: grind", "llm (2 attempts)", "sketch"
    attempts: int = 0
    elapsed: float = 0.0
    proof: str = ""
    error: str = ""  # last Lean error / checker reason
    axioms: list[str] = field(default_factory=list)


@dataclass
class SpecResult:
    name: str
    kind: str  # postcondition | property
    english: str
    lean: str
    approval: str  # approved | auto-approved | rejected
    proof: ProofResult | None = None
    mutants_caught: int = 0
    vacuity_tried: int = 0
    vacuity_rejected: int = 0
    impl_violations: int = 0

    @property
    def possibly_vacuous(self) -> bool:
        """No evidence the spec constrains anything: it accepted every perturbed output,
        caught no injected bug, and was never violated by the implementation."""
        return (
            self.kind == "postcondition"
            and self.vacuity_tried >= 10
            and self.vacuity_rejected == 0
            and self.mutants_caught == 0
            and self.impl_violations == 0
        )


@dataclass
class FixProposal:
    explanation: str
    diff: str
    validated: bool
    validation: str = ""
    patch_path: str = ""
    applied: bool = False


@dataclass
class Finding:
    kind: str  # spec_violation | divergence | crash | timeout | type
    confidence: str  # confirmed | likely | possible
    args_repr: str
    impl: str
    model: str
    violated_specs: list[str] = field(default_factory=list)
    explanation: str = ""
    count: int = 1  # how many sampled inputs showed this kind of failure
    fix: FixProposal | None = None

    def title(self) -> str:
        return {
            "spec_violation": "Implementation violates an approved spec",
            "divergence": "Implementation disagrees with the verified model",
            "crash": "Implementation raises an exception on a valid input",
            "timeout": "Implementation does not terminate on a valid input",
            "type": "Implementation returns a value of the wrong type",
        }.get(self.kind, self.kind)


@dataclass
class MutationSummary:
    total: int = 0
    killed: int = 0
    killed_by_specs: int = 0
    survivors: list[str] = field(default_factory=list)
    likely_equivalent: int = 0
    inputs: int = 0

    @property
    def score(self) -> float:
        return self.killed / self.total if self.total else 0.0

    @property
    def adjusted_score(self) -> float:
        """Excluding survivors that also agreed with the model on ~1000 extra inputs."""
        denom = self.total - self.likely_equivalent
        return self.killed / denom if denom else 1.0


@dataclass
class Report:
    function: str
    file: str
    verdict: str = "error"  # passed | bug | partial | error
    headline: str = ""
    understanding: str = ""
    precondition: str = ""
    specs: list[SpecResult] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    drt: dict = field(default_factory=dict)
    mutation: MutationSummary | None = None
    model_revisions: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error: str = ""
    cost_usd: float = 0.0  # actual spend this run
    nominal_cost_usd: float = 0.0  # cost including cached calls
    llm_calls: int = 0
    elapsed_s: float = 0.0
    stage_seconds: dict = field(default_factory=dict)
    cost_by_stage: dict = field(default_factory=dict)
    artifacts_dir: str = ""
    config: dict = field(default_factory=dict)

    # -- derived ---------------------------------------------------------------------------
    @property
    def active_specs(self) -> list[SpecResult]:
        return [s for s in self.specs if s.approval != "rejected"]

    @property
    def proved(self) -> int:
        return sum(1 for s in self.active_specs if s.proof and s.proof.status == "proved")

    @property
    def bug_reported(self) -> bool:
        return any(f.confidence in ("confirmed", "likely") for f in self.findings)

    def to_json(self) -> dict:
        d = asdict(self)
        d["proved"] = self.proved
        d["total_specs"] = len(self.active_specs)
        d["bug_reported"] = self.bug_reported
        return d

    def save(self, directory: Path) -> tuple[Path, Path]:
        directory.mkdir(parents=True, exist_ok=True)
        jp = directory / "report.json"
        jp.write_text(json.dumps(self.to_json(), indent=2, default=str))
        mp = directory / "report.md"
        mp.write_text(self.to_markdown())
        return jp, mp

    def to_markdown(self) -> str:
        L = [f"# Larch report: `{self.function}` ({self.file})", ""]
        L += [f"**Verdict: {self.verdict.upper()}**: {self.headline}", ""]
        if self.understanding:
            L += ["## Intended behaviour", self.understanding, ""]
        L += ["## Specs", f"Precondition: {self.precondition or 'none'}", ""]
        L += ["| # | spec | status | English |", "|---|---|---|---|"]
        for i, s in enumerate(self.specs, 1):
            st = s.proof.status if s.proof else "-"
            if s.approval == "rejected":
                st = "rejected"
            L.append(f"| {i} | `{s.name}` ({s.kind}) | {st} | {s.english} |")
        L.append("")
        if self.findings:
            L.append("## Findings")
            for f in self.findings:
                L += [
                    f"### {f.title()} ({f.confidence})",
                    f"- input: `{self.function}({f.args_repr})`",
                    f"- implementation: `{f.impl}`",
                    f"- verified model: `{f.model}`",
                ]
                if f.violated_specs:
                    L.append(f"- violates: {', '.join(f.violated_specs)}")
                if f.explanation:
                    L.append(f"- {f.explanation}")
                if f.fix:
                    L += ["", "Proposed fix" + (" (validated against the model)" if f.fix.validated else " (NOT validated)") + ":", "```diff", f.fix.diff, "```"]
                L.append("")
        if self.drt:
            L += ["## Differential testing", f"{self.drt.get('valid', 0)} valid inputs, {self.drt.get('disagreements', 0)} disagreements.", ""]
        if self.mutation:
            m = self.mutation
            L += ["## Mutation analysis", f"{m.killed}/{m.total} mutants detected ({m.score:.0%}); {m.killed_by_specs} by the specs alone."]
            for s in m.survivors:
                L.append(f"- survived: {s}")
            L.append("")
        if self.warnings:
            L += ["## Warnings"] + [f"- {w}" for w in self.warnings] + [""]
        L += [f"Cost ${self.cost_usd:.3f} · {self.elapsed_s:.0f}s · {self.llm_calls} LLM calls"]
        return "\n".join(L) + "\n"
