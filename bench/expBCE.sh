#!/bin/sh
# Follow-ups on the winning formalization mode (arg 1):
#   E: test generation (typed / llm; "mixed" is the Experiment A default)  [formalization cached]
#   X: documented examples on/off                                          [new prompt]
#   C: reasoning effort for formalization (low; A used medium)
#   B: model choice (Haiku 4.5, Opus 5; A used Sonnet 5)
set -u
MODE=${1:-hybrid}
PY=.venv/bin/python
BASE="--formalize-mode $MODE --no-proofs --no-mutation --no-fix --parallel 6"
$PY -m bench.run --name E-typed  --model claude-sonnet-5 --effort medium --test-strategy typed $BASE
$PY -m bench.run --name E-llm    --model claude-sonnet-5 --effort medium --test-strategy llm $BASE
$PY -m bench.run --name X-examples --model claude-sonnet-5 --effort medium --doc-examples $BASE
$PY -m bench.run --name C-low    --model claude-sonnet-5 --effort low $BASE
$PY -m bench.run --name B-haiku  --model claude-haiku-4-5 --effort medium $BASE
$PY -m bench.run --name B-opus   --model claude-opus-5 --effort medium $BASE
echo "EXPERIMENTS B/C/E/X DONE"
