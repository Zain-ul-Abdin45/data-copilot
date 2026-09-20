#!/bin/sh
# The whole night in one command: full preflight, then MetricFlow, then Wren, then a
# side-by-side comparison. Nothing runs if the preflight fails.
#
#     sh evals/night_all.sh              # 2 runs per case per engine
#     sh evals/night_all.sh --repeat 1   # shorter
#
# Everything printed is also kept in evals/results/<stamp>_log.txt. Ctrl+C stops the whole
# night and keeps the partial results. In the morning:  cat evals/results/<stamp>_compare.txt
cd "$(dirname "$0")/.." || exit 1
mkdir -p evals/results
STAMP=$(date +%Y-%m-%d_%H%M)
A="evals/results/${STAMP}_metricflow.json"
B="evals/results/${STAMP}_wren.json"
LOG="evals/results/${STAMP}_log.txt"
EXTRA="$*"
trap 'echo "Interrupted: stopping. Partial results are in evals/results/."; exit 130' INT

run_eval() {  # $1 = name, $2 = results file, the rest = extra run_evals arguments for this engine
    name="$1"; out="$2"; shift; shift
    status="${out%.json}.exitcode"
    { caffeinate -i nice -n 19 .venv/bin/python -u evals/run_evals.py --repeat 2 --pause 20 \
          --out "$out" --skip-model-check "$@" $EXTRA; echo $? > "$status"; } 2>&1 | tee -a "$LOG"
    code=$(cat "$status" 2>/dev/null || echo "?")
    rm -f "$status"
    echo "--- $name eval exited with code $code" | tee -a "$LOG"
    if [ ! -f "$out" ]; then
        echo "!!! $name saved NO results file. It stopped before its first question; the reason is in the output above and in $LOG" | tee -a "$LOG"
    fi
}

nice -n 15 .venv/bin/python evals/preflight.py || { echo "Preflight failed: nothing was run."; exit 1; }
if pmset -g batt 2>/dev/null | grep -q "Battery Power"; then
    echo "!!! WARNING: running on BATTERY. Plug in the charger: a long model run drains it and the Mac will sleep."
fi
echo "Keep the lid OPEN. Closing it puts the Mac to sleep even under caffeinate (the screen may dim or lock)."
echo "Started $(date +%H:%M). Log: $LOG"
echo "The first question of each engine loads the model: about 40 s of silence is normal."

echo "=== MetricFlow ===" | tee -a "$LOG"
run_eval MetricFlow "$A"
echo "=== Wren ===" | tee -a "$LOG"
run_eval Wren "$B" --agent ../wren-bakeoff/agent_wren.py:ask

echo "=== Comparison ===" | tee -a "$LOG"
.venv/bin/python evals/compare.py "$A" "$B" 2>&1 | tee "evals/results/${STAMP}_compare.txt"
echo "Finished $(date +%H:%M)."
