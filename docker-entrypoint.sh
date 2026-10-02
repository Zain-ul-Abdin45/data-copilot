#!/bin/sh
# Builds the warehouse (idempotent: safe on every container start, not just the first) then
# starts the UI. Split out of ui/run.sh because that script assumes a host Postgres/Ollama
# already running and binds to 127.0.0.1 only; here Postgres is a compose service and the UI
# must bind 0.0.0.0 to be reachable from outside the container.
set -e

if [ "$COPILOT_UI_STUB" != "1" ]; then
  echo "Waiting for Postgres at ${COPILOT_DB_HOST:-postgres}..."
  until pg_isready -h "${COPILOT_DB_HOST:-postgres}" -U postgres -d data_copilot -q; do
    sleep 1
  done

  echo "Seeding raw tables..."
  python seed_db.py

  echo "Building the warehouse (dbt seed/run/test)..."
  cd dbt-test-project
  dbt seed && dbt run && dbt test
  cd ..
fi

python ui/harden.py

echo "Starting the UI on http://0.0.0.0:8000 ..."
exec chainlit run ui/app.py --host 0.0.0.0 --port 8000 --headless
