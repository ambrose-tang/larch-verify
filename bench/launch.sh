#!/bin/sh
# Run a benchmark script fully detached (own session), so it survives the
# terminal/agent that started it. Usage: bench/launch.sh bench/expA.sh LOGFILE [args...]
script=$1; log=$2; shift 2
nohup python3 -c 'import os, sys; os.setsid(); os.execvp(sys.argv[1], sys.argv[1:])' "$script" "$@" >> "$log" 2>&1 &
echo "launched $script (pid $!) -> $log"
