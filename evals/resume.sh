#!/bin/sh
# Continue an interrupted night_all.sh run. Finished runs are kept; only the missing ones are done.
#
#     sh evals/resume.sh 2026-09-20_0150            # the stamp in the results file names
#     sh evals/resume.sh 2026-09-20_0150 --pause 5  # extra args go to run_evals.py
#
# The engines, --repeat and settings must be the same as in the original run (they are checked).
cd "$(dirname "$0")/.." || exit 1
STAMP="$1"
[ -n "$STAMP" ] || { echo "usage: sh evals/resume.sh <stamp>   e.g. 2026-09-20_0150 (see evals/results/)"; exit 1; }
shift
A="evals/results/${STAMP}_metricflow.json"
B="evals/results/${STAMP}_wren.json"
LOG="evals/results/${STAMP}_log.txt"
EXTRA="$*"
[ -f "$A" ] || [ -f "$B" ] || { echo "No results for stamp $STAMP in evals/results/."; exit 1; }
trap 'echo "Interrupted: stopping. Partial results are in evals/results/."; exit 130' INT

run_eval() {  # $1 = name, $2 = results file, the rest = extra run_evals arguments for this engine
    name="$1"; out="$2"; shift; shift
    [ -f "$out" ] || { echo "--- $name: no results file, nothing to resume" | tee -a "$LOG"; return; }
    status="${out%.json}.exitcode"
    { caffeinate -i nice -n 19 .venv/bin/python -u evals/run_evals.py --repeat 2 --pause 20 \
          --resume "$out" --skip-model-check "$@" $EXTRA; echo $? > "$status"; } 2>&1 | tee -a "$LOG"
    code=$(cat "$status" 2>/dev/null || echo "?")
    rm -f "$status"
    echo "--- $name eval exited with code $code" | tee -a "$LOG"
}

nice -n 15 .venv/bin/python evals/preflight.py --fast || { echo "Preflight failed: fix that first."; exit 1; }
if pmset -g batt 2>/dev/null | grep -q "Battery Power"; then
    echo "!!! WARNING: running on BATTERY. Plug in the charger: a model run drains it and the Mac will sleep."
fi
echo "Keep the lid OPEN. Closing it puts the Mac to sleep even under caffeinate."
echo "Resuming $STAMP at $(date +%H:%M). Log: $LOG" | tee -a "$LOG"

echo "=== MetricFlow ===" | tee -a "$LOG"
run_eval MetricFlow "$A"
echo "=== Wren ===" | tee -a "$LOG"
run_eval Wren "$B" --agent ../wren-bakeoff/agent_wren.py:ask

echo "=== Comparison ===" | tee -a "$LOG"
.venv/bin/python evals/compare.py "$A" "$B" 2>&1 | tee "evals/results/${STAMP}_compare.txt"
echo "Finished $(date +%H:%M)."
