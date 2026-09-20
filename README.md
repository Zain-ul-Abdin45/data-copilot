# Data Copilot

A self-hosted assistant for questions about a Postgres warehouse. It answers
from governed metrics first and says so when it cannot.

```
question ─► Qwen3 (Ollama) ─► describe_metrics ─┐
                              query_metric ─────┼─► MetricFlow ─► Postgres (read-only role)
                              search_catalog ───┤   (dbt semantic layer)
                              run_sql ──────────┴─► sqlglot guard ─► Postgres (read-only role)
```

| Piece | Job | Open-source project |
|---|---|---|
| Metric definitions | "revenue" means one thing | dbt + MetricFlow (Apache 2.0) |
| Business rules | which statuses count as revenue, glossary | CSV seeds in `dbt-test-project/seeds/` |
| Catalog | what tables, columns and metrics exist | dbt manifest + `information_schema` |
| Fallback SQL | questions no metric covers | sqlglot validation, read-only role |
| Model | tool calling | Qwen3 via Ollama (swap `llm.py` for vLLM) |

What the code (not the model) guarantees on every answer:

- **Footer** "How this was calculated", built from the tool trace: governed metric
  or ad-hoc SQL, with the filters and the query.
- **Result tables** drawn from the returned rows, so a multi-row result is never
  retyped by the model.
- **Grounding check**: every figure in the model's text must appear in a tool
  result or be a sum, difference or share of returned figures. An invented figure
  triggers one automatic correction; if it persists the answer carries a warning.
- **Fast first**: thinking is off (~15 s). An answer that used ad-hoc SQL, ended on
  an error or still has unverified figures is re-run once with thinking on (~3 min).
- **Audit log** `logs/audit.jsonl`: question, tools, SQL, row counts. Never rows or
  answer text. `python audit.py recent`, and `python audit.py fallbacks` lists the
  questions that needed ad-hoc SQL: the backlog of metrics worth governing.

## Run it

```
python -m venv .venv && .venv/bin/pip install -r requirements.txt
createdb data_copilot && psql -d data_copilot -f setup_db.sql
.venv/bin/python seed_db.py                       # raw tables
cd ../dbt-test-project && ../data-copilot/.venv/bin/dbt seed && \
  ../data-copilot/.venv/bin/dbt run && ../data-copilot/.venv/bin/dbt test
cd ../data-copilot
ollama pull qwen3:14b
.venv/bin/python agent.py "What is our net revenue by month?"
.venv/bin/uvicorn main:app                          # POST /ask {"question": "..."}
```

Settings are environment variables (see `settings.py`): `COPILOT_MODEL`,
`COPILOT_THINK`, `COPILOT_DB_*`, `DBT_PROJECT_DIR`.

## Changing business rules

Edit the CSVs in `../dbt-test-project/seeds/` (see the README there), rebuild
with dbt, then run `python evals/check_semantic_layer.py`.

## Tests

One command checks everything that does not need the model (about 17 seconds, never
generates a token; Ollama is only asked which models are installed):

```
sh evals/preflight.sh          # full
sh evals/preflight.sh --fast   # unit tests, database, Ollama, golden set: under a second
```

It runs, in order: the stubbed unit tests, the agent role's database scope (reads
analytics, cannot write, cannot see `raw`), Ollama and model presence, consistency of
`golden.yaml` (every truth query runs), the night scripts, dbt tests, the glossary, an
**oracle run** for each engine, and a crash-resilience test of the eval runner.

The oracle is a scripted "perfect model" driving the real agent loop, the real tools
and the real grader. If it passes, a night-run failure is the model's behaviour, not a
broken harness. If it fails, the engine check that pinpoints the cause runs
automatically. Known engine gaps (Wren: top-N sorting, month labels) are declared in
`evals/oracle.py` and must fail exactly as declared.

The model runs (heavy) are separate:

```
sh evals/night_all.sh                  # preflight, MetricFlow, Wren, then a comparison
sh evals/night.sh                      # one engine (fast preflight first)
sh evals/night.sh --agent ../wren-bakeoff/agent_wren.py:ask
python evals/run_evals.py -k refund    # a subset, interactively
```

Nothing starts if the preflight fails. Runs go at lowest priority with `caffeinate`, rest
between questions, save after every run, and record a crashed question as a failure
instead of stopping. Everything printed is also kept in `evals/results/<stamp>_log.txt`.
Ctrl+C stops the whole night and keeps the partial results. If an engine stops before its
first question, the script prints its exit code and says so ("saved NO results file")
instead of failing later in the comparison. The first question loads the model, so about
40 s pass with no output; that is normal. Read `evals/results/<stamp>_compare.txt` in the morning; a PASS on a
`rubric` case only means the right tools ran and the figures were grounded, so read those
answers. An interrupted run is continued, not restarted, with `sh evals/resume.sh <stamp>` (the timestamp in the
results file names): finished runs are kept, a half-done case continues after its saved runs, and
settings that differ from the original run are refused. Timings exclude Mac sleep (`elapsed_s`; the
wall time is `wall_s`). Individual pieces: `python tests/run_light.py`, `python evals/oracle.py --engine
metricflow|wren`, `python evals/check_semantic_layer.py`, `python evals/compare.py A.json B.json`.

## Model choice (measured on evals/golden.yaml, 15 cases, 18 GB laptop)

| Model | Thinking | Passed | Time for 15 cases | Notes |
|---|---|---|---|---|
| qwen3:8b | off | 12 | fast | invented a refund figure by multiplying two metrics |
| qwen3:8b | on | 12-13 | ~20 min | flaky on the ungoverned fallback |
| qwen3:14b | on | 14 | 46 min (~3 min/question) | most careful; `open_discount` honest but gives no baseline |
| qwen3:14b | off | 14 | under 4 min (~15 s/question) | labelled gross revenue as "net" in the ungoverned fallback; `open_discount` answer meaningless |

Both 14B runs got all 9 governed-metric cases right; the 8B runs missed one or
two of them (depending on the run). Between the two 14B runs the differences
are in the ungoverned fallback and open-ended questions. One run each, so
treat single-case differences as noise. Set with `COPILOT_MODEL`, `COPILOT_THINK`
and `COPILOT_ESCALATE`. These runs pre-date the grounding check, the code-drawn
tables and fast-first escalation, so they need re-measuring (`sh evals/night.sh`).

## Known limits

- The grounding check finds invented figures. It cannot tell whether allowed
  arithmetic is meaningful (a share of the wrong two numbers passes).
- The model can add filters nobody asked for (such as a date range). The footer
  always shows the filters that were applied.
- `search_catalog` is keyword-based; it is small enough not to need embeddings yet.
- Result rows reach the model, including personal data such as customer emails.
  Removing that is the job of the separate dataveil package (planned).
