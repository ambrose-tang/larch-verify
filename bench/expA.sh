#!/bin/sh
# Experiment A: formalization approach (Sonnet 5, medium effort), bug detection only.
set -u
PY=.venv/bin/python
COMMON="--model claude-sonnet-5 --effort medium --no-proofs --no-mutation --no-fix --parallel 6"
$PY -m bench.run --name A-hybrid  --formalize-mode hybrid $COMMON
$PY -m bench.run --name A-intent  --formalize-mode intent $COMMON
$PY -m bench.run --name A-translit --formalize-mode transliterate $COMMON
echo "EXPERIMENT A DONE"
