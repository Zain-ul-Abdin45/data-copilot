#!/bin/sh
# Gentle overnight eval of ONE engine: fails fast if the setup is broken, then runs at lowest
# CPU priority, stops the Mac sleeping mid-run, rests between questions, and saves as it goes.
#
#     sh evals/night.sh                                                  # MetricFlow, 15 cases x 2 runs
#     sh evals/night.sh --agent ../wren-bakeoff/agent_wren.py:ask       # Wren
#     sh evals/night.sh --repeat 1                                       # extra args go to run_evals.py
#
# Everything printed is also kept in evals/results/<stamp>_log.txt. Ctrl+C stops the run and
# keeps the partial results. Both engines and a comparison in one go:  sh evals/night_all.sh
cd "$(dirname "$0")/.." || exit 1
mkdir -p evals/results
STAMP=$(date +%Y-%m-%d_%H%M)
LOG="evals/results/${STAMP}_night_log.txt"
trap 'echo "Interrupted: stopping. Partial results are in evals/results/."; exit 130' INT

nice -n 15 .venv/bin/python evals/preflight.py --fast || { echo "Preflight failed: fix that before sleeping."; exit 1; }
if pmset -g batt 2>/dev/null | grep -q "Battery Power"; then
    echo "!!! WARNING: running on BATTERY. Plug in the charger: a long model run drains it and the Mac will sleep."
fi
echo "Keep the lid OPEN. Closing it puts the Mac to sleep even under caffeinate (the screen may dim or lock)."
echo "Log: $LOG   (first question loads the model: ~40 s of silence is normal)"
caffeinate -i nice -n 19 .venv/bin/python -u evals/run_evals.py --repeat 2 --pause 20 "$@" 2>&1 | tee -a "$LOG"
