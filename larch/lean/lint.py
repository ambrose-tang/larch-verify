"""Reject LLM-written Lean that could make a proof unsound or unfaithful.

This is defence in depth. The authoritative check is the out-of-process checker
(larch/lean/checker.py), which loads the compiled environment and inspects kernel
terms: axioms used and exact theorem statements. The lint runs first and gives the
model a clear, early error for constructs we never accept:

* incomplete proofs: sorry, admit, sorryAx
* new trust: axiom, native_decide / ofReduceBool / trustCompiler
* code the kernel does not see: implemented_by, extern, unsafe, partial, opaque
  (they let the compiled model used in differential testing differ from the
  logical model the proofs are about)
* meta-programming that could change what later syntax means: macro, syntax,
  notation, elab, run_cmd, #eval, initialize, ...
* imports and non-allowlisted set_option (debug.skipKernelTC, ...)
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_ALWAYS_FORBIDDEN = {
    "sorry": "`sorry` leaves a proof unfinished",
    "admit": "`admit` leaves a proof unfinished",
    "sorryAx": "`sorryAx` is the axiom behind sorry",
    "axiom": "new axioms are not allowed",
    "native_decide": "`native_decide` trusts the compiler (adds an axiom); use `decide` or a real proof",
    "ofReduceBool": "`Lean.ofReduceBool` trusts the compiler",
    "trustCompiler": "`Lean.trustCompiler` trusts the compiler",
    "implemented_by": "`@[implemented_by]` makes runtime code differ from the logical definition",
    "extern": "`@[extern]` makes runtime code differ from the logical definition",
    "export": "`@[export]` is not allowed",
    "unsafe": "`unsafe` code is not allowed",
    "partial": "`partial def` is opaque to proofs; use structural recursion or `termination_by`",
    "opaque": "`opaque` definitions cannot be reasoned about",
    "elab": "meta-programming is not allowed",
    "elab_rules": "meta-programming is not allowed",
    "macro": "macros are not allowed",
    "macro_rules": "macros are not allowed",
    "syntax": "syntax extensions are not allowed",
    "declare_syntax_cat": "syntax extensions are not allowed",
    "notation": "notation is not allowed (it can change what later statements mean)",
    "infix": "notation is not allowed",
    "infixl": "notation is not allowed",
    "infixr": "notation is not allowed",
    "prefix": "notation is not allowed",
    "postfix": "notation is not allowed",
    "run_cmd": "running commands is not allowed",
    "run_elab": "running elaborators is not allowed",
    "run_meta": "running meta code is not allowed",
    "initialize": "initializers are not allowed",
    "builtin_initialize": "initializers are not allowed",
    "import": "imports are managed by Larch",
    "debug": "debug options are not allowed",
}

# Hash-commands that could execute code at elaboration time.
_FORBIDDEN_HASH = {"#eval", "#exit", "#guard_msgs"}

_ALLOWED_OPTION_PREFIXES = (
    "maxHeartbeats",
    "maxRecDepth",
    "synthInstance.maxHeartbeats",
    "synthInstance.maxSize",
    "linter.",
    "pp.",
    "trace.",
    "grind.",
    "exponentiation.threshold",
    "diagnostics",
)

# Extra constructs forbidden only in the model file: the spec statements live there,
# so instances could change what `≤` or `==` mean inside an approved spec.
_MODEL_ONLY_FORBIDDEN = {
    "instance": "custom instances are not allowed in the model; use `deriving`",
    "attribute": "attribute changes are not allowed in the model",
}


@dataclass
class LintIssue:
    line: int
    token: str
    reason: str

    def __str__(self) -> str:
        return f"line {self.line}: `{self.token}` — {self.reason}"


def strip_comments_and_strings(src: str) -> str:
    """Blank out comments (nested /- -/ and --) and string/char literals, keeping
    line structure so reported line numbers stay correct."""
    out = []
    i = 0
    n = len(src)
    depth = 0
    while i < n:
        c = src[i]
        nxt = src[i + 1] if i + 1 < n else ""
        if depth > 0:
            if c == "/" and nxt == "-":
                depth += 1
                out.append("  ")
                i += 2
            elif c == "-" and nxt == "/":
                depth -= 1
                out.append("  ")
                i += 2
            else:
                out.append("\n" if c == "\n" else " ")
                i += 1
            continue
        if c == "/" and nxt == "-":
            depth = 1
            out.append("  ")
            i += 2
            continue
        if c == "-" and nxt == "-":
            while i < n and src[i] != "\n":
                out.append(" ")
                i += 1
            continue
        if c == '"':
            out.append(" ")
            i += 1
            while i < n and src[i] != '"':
                if src[i] == "\\" and i + 1 < n:
                    out.append("  ")
                    i += 2
                    continue
                out.append("\n" if src[i] == "\n" else " ")
                i += 1
            out.append(" ")
            i += 1
            continue
        if c == "'" and i + 2 < n and (src[i + 2] == "'" or (src[i + 1] == "\\" and i + 3 < n)):
            # char literal like 'a' or '\n' (Lean also uses ' in identifiers like x')
            prev = src[i - 1] if i > 0 else " "
            if not (prev.isalnum() or prev in "_'!?"):
                end = src.find("'", i + 2)
                if end != -1 and end - i <= 8:
                    out.append(" " * (end - i + 1))
                    i = end + 1
                    continue
        out.append(c)
        i += 1
    return "".join(out)


_IDENT = re.compile(r"(?<![A-Za-z0-9_.'!?])([A-Za-z_][A-Za-z0-9_'!?]*(?:\.[A-Za-z_][A-Za-z0-9_'!?]*)*)")
_HASH = re.compile(r"(#[A-Za-z_]+)")
_SET_OPTION = re.compile(r"\bset_option\s+([A-Za-z0-9_.]+)")


def lint_lean(src: str, *, model_file: bool = False) -> list[LintIssue]:
    clean = strip_comments_and_strings(src)
    issues: list[LintIssue] = []
    forbidden = dict(_ALWAYS_FORBIDDEN)
    if model_file:
        forbidden.update(_MODEL_ONLY_FORBIDDEN)
    for lineno, line in enumerate(clean.splitlines(), start=1):
        for m in _IDENT.finditer(line):
            ident = m.group(1)
            parts = ident.split(".")
            for part in parts:
                if part in forbidden:
                    # `debug` and friends are only dangerous as option names; handled below.
                    if part == "debug":
                        continue
                    issues.append(LintIssue(lineno, ident, forbidden[part]))
                    break
        for m in _HASH.finditer(line):
            if m.group(1) in _FORBIDDEN_HASH:
                issues.append(LintIssue(lineno, m.group(1), "commands that execute code are not allowed"))
        for m in _SET_OPTION.finditer(line):
            opt = m.group(1)
            if not opt.startswith(_ALLOWED_OPTION_PREFIXES):
                issues.append(LintIssue(lineno, f"set_option {opt}", "this option is not allowed"))
    return issues
