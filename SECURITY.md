# Security and data handling

## What leaves your machine
Larch sends the **function under test, its documentation, the imports and top-level
definitions it refers to**, and generated Lean code to the configured LLM provider
(Anthropic API, Amazon Bedrock, Google Vertex AI, or your Claude Code login). Nothing
else from the repository is sent. Every prompt and response is written to
`~/.cache/larch/runs/<run>/llm/` so you can audit exactly what was sent.

To keep traffic inside your cloud account, use `--provider bedrock` or `--provider vertex`;
`ANTHROPIC_BASE_URL` routes API traffic through a corporate gateway.

## What runs on your machine
Larch **executes the function under test** (and mutated or fixed variants of it) on
generated inputs, in a separate process using your project's interpreter or Node.js.
That process has per-call timeouts and its output is redirected, but it is **not a
security sandbox**: it runs with your user's permissions, like your unit tests do.
Only verify code you would run your test suite on. In CI, run Larch in the same
isolated job you use for tests.

Larch writes nothing into your repository unless you run `larch init` (approved specs in
`.larch/specs/`) or confirm `--apply` for a validated fix.

## Reporting a vulnerability
Please open a private security advisory on the GitHub repository rather than a public issue.
