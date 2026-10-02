# Changelog

## Unreleased

### Added
- **LARCH.md**: the project's contracts in plain English (optional exact Lean blocks),
  one file at the repository root. `larch verify` with no arguments checks everything
  in it; `larch init` drafts it from the best candidates of `larch scan`. Each bullet
  becomes a mandatory spec; the review shows the developer's words next to Larch's
  reading of its Lean. Headings without bullets get proposed contracts, which are
  written back once approved. Changing a bullet triggers re-formalization.
- **Contract fidelity checks**: for each formalized contract, concrete outputs judged
  from the English alone (one violating, one satisfying) are evaluated against the
  Lean statement; disagreements are repaired before review.
- **Exhaustive testing**: when the precondition bounds every input to a small box,
  every input is run (smallest first) and the box is proved in Lean to contain every
  valid input (`input_domain`). A function whose postconditions hold on every input of
  a proved-complete domain passes even where a model proof is missing.
- **Stateful components**: a class under a LARCH.md heading (or `larch verify
  file::Class`) is modelled as a Lean state machine. Invariants are proved for every
  reachable state, operation contracts for every call from every such state. The real
  object is tested with random and bounded-exhaustive call sequences, comparing results
  and observers after every call; failures shrink to the shortest sequence. Mutation
  analysis and validated fixes cover classes too. Python and TypeScript/JavaScript.
- **Services**: `## service NAME` in LARCH.md verifies a running HTTP service. Larch
  starts a throwaway database (Postgres via Docker or local binaries, SQLite, none, or a
  URL), starts the service, reads its OpenAPI description and route source, and models
  it as a state machine whose operations are endpoints (status code plus selected
  response fields). Request sequences are compared with the model after a database
  reset; ids are deterministic. Mutants and fixes run on a copy of the project on its
  own port. `larch verify "service orders"` or `larch verify` with no arguments.
- **System rules**: bullets under `# System rules` with `(uses: A, B)` are stated in Lean
  over the composed component models and proved from the components' contracts
  (assume–guarantee). The report lists the contracts the rule rests on and their status.
- **Coverage-guided sequences**: half of the call/request sequences are grown from the
  ones that reach the rarest model behaviour, so deep states are tested routinely.
  Failing sequences shrink by greedy call deletion as well as by Hypothesis.
- **Model revision for classes and services**: a disagreement adjudicated as a model
  bug revises the model and re-tests, as for functions.
- **Several bugs in one class or service are reported separately**: disagreements are
  grouped by the failing operation and the outcomes on each side, and each group gets
  its own shrunk sequence. A fix is accepted when the disagreements it targets are gone
  and every remaining one also occurs, identically, in the original code.
- `larch scan` lists classes as candidates (exception classes excluded).
- `examples/shop`: a storefront backend (shipping rates, discounts, pay-in-N split in
  TypeScript, a store-credit ledger, a cart, and a FastAPI + Postgres orders service)
  with a LARCH.md, system rules and realistic seeded bugs.

### Fixed
- Model results a JavaScript runtime cannot represent exactly (beyond 2^53) are outside
  the compared domain instead of reported as disagreements.
- Formalization errors in reports show the Lean error, not only its heading.
- A mutant or fix of a service that fails to start no longer stops the database the
  other runs share (which made later mutants look detected).

## 0.2.0

### Fixed
- **`ModuleNotFoundError` when verifying code.** The worker ran in the project's
  interpreter with hypothesis and its dependencies borrowed from Larch's own
  environment, which broke whenever the two Python versions differed (most recently on
  hypothesis's compiled `_native` module). Test generation now runs in Larch's own
  interpreter and the code under test runs behind a standard-library-only adapter in
  the project's interpreter (CPython 3.7+). See DECISIONS.md D9.
- The project's interpreter is found the way its tooling would find it: in-project
  virtualenvs up to the repository root, `$VIRTUAL_ENV`, conda, poetry, pipenv, pdm,
  hatch, `$UV_PROJECT_ENVIRONMENT`, then PATH. Larch's own interpreter is a warned last resort.
- The user's `PYTHONPATH` is no longer overwritten; project roots, `src/` layouts,
  implicit namespace packages, pytest's `pythonpath` and `[tool.larch] pythonpath` are importable.
- An input that crashes or hangs the interpreter is reported as a crash/timeout
  finding; the run continues.
- Mutants that do not load in the project's runtime are excluded from the mutation
  score instead of being counted as caught.
- `[tool.larch]` / `.larch.toml` are found above the file, up to the repository root.

### Added
- **JavaScript and TypeScript** (`.js .mjs .cjs .ts .mts .cts`), on Node.js 20.6+
  (TypeScript: 22.6+, built-in type stripping). ESM and CommonJS, non-exported functions,
  static methods, JSDoc types for plain JavaScript.
- Preflight: the module is loaded in the project's runtime before any LLM call, and an
  import failure explains the missing package and which interpreter has it.
- Fixes are validated by loading them in the project's runtime; the fix prompt states
  the runtime version and forbids new third-party imports.
- `larch scan`: find and rank verifiable functions across a repository, with reasons for skips.
- `larch verify DIR`, `--changed [REF]`, `--limit`, `--approved-only`.
- `--sarif`, `--junit`, `--markdown` outputs; a composite GitHub Action (`action.yml`).
- Amazon Bedrock and Google Vertex AI providers (`--provider bedrock|vertex`, or
  `CLAUDE_CODE_USE_BEDROCK` / `CLAUDE_CODE_USE_VERTEX`).
- `larch doctor` reports the interpreter that will run your code and why.

### Changed
- `exceptiongroup` is no longer a dependency.

## 0.1.0
- Initial release: Python functions, Lean 4 models, proofs, differential testing,
  mutation analysis and validated fixes.
