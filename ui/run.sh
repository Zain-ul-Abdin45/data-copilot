#!/bin/sh
# Start the interface on localhost only.   sh ui/run.sh     (PORT=8001 sh ui/run.sh for another port)
cd "$(dirname "$0")" || exit 1
pg_isready -q || echo "Warning: Postgres is not accepting connections."
curl -s localhost:11434/api/version >/dev/null || echo "Warning: Ollama is not running (open -a Ollama)."
../.venv/bin/python harden.py
exec ../.venv/bin/chainlit run app.py --host 127.0.0.1 --port "${PORT:-8000}" --headless
