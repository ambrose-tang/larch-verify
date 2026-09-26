#!/bin/sh
.venv/bin/python -m bench.run --name D-portfolio --formalize-mode hybrid --model claude-sonnet-5 --effort medium --variants correct --proof-strategy portfolio --no-mutation --no-fix --parallel 3
echo "D-portfolio DONE"
