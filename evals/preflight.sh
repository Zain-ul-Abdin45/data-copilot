#!/bin/sh
# Everything checkable without running the model. Lowest-effort CPU priority.
#     sh evals/preflight.sh           # full, about a minute
#     sh evals/preflight.sh --fast    # a few seconds
cd "$(dirname "$0")/.." || exit 1
exec nice -n 15 .venv/bin/python evals/preflight.py "$@"
