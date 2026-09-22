# Data Copilot

A self-hosted assistant for questions about a warehouse — Postgres, a local DuckDB file, or
(unverified) Trino. It answers from governed metrics first and says so when it cannot.

```
question ─► Qwen3 (Ollama) ─► describe_metrics ─┐
                              query_metric ─────┼─► MetricFlow ─► dbt profile target ──┐
                              search_catalog ───┤   (dbt semantic layer)               │
                              run_sql ──────────┼─► sqlglot guard ─► datasources/ ──────┴─► Postgres | DuckDB | Trino
                              derive ───────────┘   (derive.py: change/share over an
                                                      already-returned result, no query)
```

Both arrows into the engine are the same switch: `COPILOT_DATASOURCE` (`postgres` | `duckdb` |
`trino`) picks a `datasources/` class for run_sql/catalog and, via `DBT_TARGET`, the matching
dbt profile target for the governed path — see **Data sources** below.

| Piece | Job | Open-source project |
|---|---|---|
| Metric definitions | "revenue" means one thing | dbt + MetricFlow (Apache 2.0) |
| Business rules | which statuses count as revenue, glossary, which columns are personal data | CSV/YAML in `dbt-test-project/` |
| Catalog | what tables, columns and metrics exist | dbt manifest + `information_schema` |
| Fallback SQL | questions no metric covers | sqlglot validation (multi-dialect), one read-only role/mode |
| Model | tool calling | Qwen3 via Ollama (swap `llm.py` for vLLM) |

What the code (not the model) guarantees on every answer:

- **Footer** "How this was calculated", built from the tool trace: governed metric
  or ad-hoc SQL, with the filters and the query.
- **Result tables** drawn from the returned rows, so a multi-row result is never
  retyped by the model.
- **Grounding check**: every figure in the model's text must appear in a tool
  result or be a sum, difference or share of returned figures. An invented figure
  triggers one automatic correction; if it persists the answer carries a warning.
- **Meaningless groupings are refused**: a ratio metric cannot be grouped or filtered by a dimension it is
  built from (`refund_rate` by status is always 0% or 100%). Derived from the measure expressions, so
  new metrics are covered automatically.
- **A change over time or a share of total is computed in code, not the model, for *any* result**:
  the `derive` tool (`derive.py`) takes the most recent query_metric/run_sql result already in the
  conversation and computes period-over-period change or share-of-total over it — no new metric
  has to be defined for "month on month" or "percentage of total" to work on a metric, a
  breakdown, or an ad-hoc SQL result nobody anticipated. Where a specific ratio *is* worth
  blessing as an exact, named number (`net_revenue_growth_mom`, `offset_window: 1 month` in
  dbt-test-project/models/marts/semantic_layer.yml), the model is told to prefer it; `derive` is
  what covers everything else, so a governed metric is never a prerequisite for "what changed" or
  "what share" to be answerable. The first period in a range has no prior period, and a boundary
  artifact of MetricFlow's time-spine join can add one spurious trailing period to an
  `offset_window` metric specifically; both come back honestly `null` and are trimmed from the
  table (`tables.py`), never shown as zero or invented.
- **"Not available" is explicit**: `describe_metrics` and `search_catalog` report words that match nothing in
  the warehouse, and `describe_metrics` searches the catalog itself when no metric matches.
- **Fast first**: thinking is off (~15 s). An answer that used ad-hoc SQL, ended on
  an error or still has unverified figures is re-run once with thinking on (~3 min).
- **Audit log** `logs/audit.jsonl`: question, tools, SQL, row counts. Never rows or
  answer text. `python audit.py recent`, and `python audit.py fallbacks` lists the
  questions that needed ad-hoc SQL: the backlog of metrics worth governing.
- **Personal data is masked, not just avoided by convention**: a column dbt's schema.yml marks
  `meta: {pii: true}` (in this project: customer name and email) comes back from every tool,
  governed or ad-hoc, as `[personal data hidden]`, with a note telling the model to refer to
  `customer_id` instead. Off with `COPILOT_MASK_PII=false`. See **Privacy** below for the gap.
- **Ad-hoc SQL can be switched off** (`COPILOT_ALLOW_RUN_SQL=false`): the tool disappears from
  what the model is offered, and the prompt itself says only governed metrics and catalog
  metadata are answerable. For a deployment where "the model might write a wrong query" is not
  an acceptable risk at all, not even behind sqlguard.

## Data sources

One engine at a time, chosen with `COPILOT_DATASOURCE` (default `postgres`); `DBT_TARGET`
follows it automatically so the governed path (MetricFlow, via the dbt profile target of the
same name in `dbt-test-project/profiles.yml`) and the ad-hoc path (`run_sql`, `search_catalog`,
via `datasources/`) always point at the same warehouse. Adding an engine means one class in
`datasources/` (`dialect`, `schema`, `query()`, `columns()`) plus one profiles.yml target.

| | Postgres | DuckDB | Trino |
|---|---|---|---|
| Status | measured (the golden set) | verified (dbt build + a real model run below) | **unverified** — no cluster in dev |
| Set with | `COPILOT_DATASOURCE=postgres` (default) | `COPILOT_DATASOURCE=duckdb` | `COPILOT_DATASOURCE=trino` |
| Read-only guarantee | role + read-only session + sqlguard | sqlguard only (see `datasources/duckdb_source.py`) | sqlguard only |
| Needs | — | `pip install duckdb dbt-duckdb` | `pip install trino dbt-trino`, real cluster |

DuckDB, tried end to end on this project's data:

```
python export_to_duckdb.py                          # copies raw.* out of Postgres into a file
cd ../dbt-test-project
DBT_TARGET=duckdb COPILOT_DUCKDB_PATH=$(pwd)/../data-copilot/warehouse.duckdb \
  ../data-copilot/.venv/bin/dbt seed && ... dbt run && ... dbt test   # 23/23 pass, same as Postgres
cd ../data-copilot
COPILOT_DATASOURCE=duckdb DBT_TARGET=duckdb .venv/bin/python agent.py "What is our net revenue?"
# -> 20,486.14 — identical to the Postgres answer, same model, same question
```

Trino (`datasources/trino.py`) is written against the documented client and Trino's
ANSI `information_schema`, but nothing in this environment can run it against a real cluster
(no Docker either). Treat it as a starting point, not a tested integration; the module's
docstring says exactly what to check first.

## Privacy

Masking (above) is name-based: it looks at each result column's bare name (after a MetricFlow
`entity__` prefix or a `table.` qualifier is stripped) against the columns dbt marks `pii: true`,
not at where the data actually came from. `SELECT email AS contact` defeats it, the same class
of gap as `search_catalog` being keyword-based rather than a promise. It covers the two paths
that exist today (`query_metric`'s dimensions, `run_sql`'s SELECT list); it is a safety net for
the common case, not a guarantee against a determined query. See `privacy.py`.

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

Settings are environment variables (see `settings.py`): `COPILOT_MODEL`, `COPILOT_THINK`,
`COPILOT_DB_*`, `DBT_PROJECT_DIR`, `COPILOT_DATASOURCE`/`DBT_TARGET` (see **Data sources**),
`COPILOT_MASK_PII`, `COPILOT_ALLOW_RUN_SQL`, `COPILOT_API_TOKEN` (a bearer token for `/ask`;
empty, the default, means no auth — the current localhost-only trust model).

## Interface

```
sh ui/run.sh                          # open http://127.0.0.1:8000  (Chainlit, self-hosted)
COPILOT_UI_STUB=1 sh ui/run.sh        # canned answers, no model needed
python ui/preview.py && open ui/preview/charts.html    # the charts on real data, light and dark
```

A chat with follow-ups ("and by month?"). Each answer shows a badge (**Governed metric**, **Ad-hoc SQL: not a
governed metric**, or **From the data catalog**), the model's words, the table drawn from the returned rows, a
chart drawn from the same rows, warnings for unverified figures, and a **How this was calculated** side panel
(definitions, every tool call, refusals, the SQL). The layout logic is in `ui/render.py` (plain Python, tested
without a browser); `ui/app.py` is a thin Chainlit wrapper.

- **Charts** follow the dataviz rules: a line for a trend, bars for categories, at most three series, one axis
  (a rate and an amount get separate charts), a legend only for two or more series, one label at the end or the
  extreme (the table carries every number). Trends are always sorted chronologically. No chart when a table is the
  better form (one row, more than 12 categories, more than 3 series).
- **Privacy:** it binds to localhost and has no login by default; set `COPILOT_UI_USER` and
  `COPILOT_UI_PASSWORD` (a single shared login, Chainlit's `password_auth_callback`) before
  putting it anywhere else. Chainlit's page
  loaded Google Fonts and a jsDelivr stylesheet on every visit; `ui/harden.py` (run by `run.sh`, re-run after any
  Chainlit reinstall) removes them, and a pre-flight check fails if they come back. Chainlit 2.12 has no telemetry
  code. Conversations are not stored (no data layer). Plotly's bundle contains dormant URLs for features not used
  (maps, Chart Studio).
- **Dark mode is selected, not automatic:** set `COPILOT_UI_THEME=dark` and `default_theme = "dark"` in
  `ui/.chainlit/config.toml`. Both palettes pass the validator; light aqua is 2.74:1 on the surface, which is why a
  table always accompanies a chart.
- Rounding is half up everywhere (6.25% shows as 6.3%), so the table agrees with how people round.

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
**oracle run** for each engine, a crash-resilience test of the eval runner, and the interface (started with the
stub agent, then driven over its socket like a browser: two turns, charts, side panel, no errors, history carried).

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

Grading also checks labels: where a truth query has a text column (payment method, status), a figure written next to that label in the
model's prose ("Credit card: 4,487.40") must be the true figure for it. A right number under the wrong label used to pass.

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
- Small prompt or description changes can flip whether the 14B model adds a `group_by` nobody asked for (it did on
  `refund_rate` and `open_discount`). Step 1 of the prompt now says so explicitly; after any wording change, re-run the
  single-number cases (`-k refund_rate`, `-k open_discount`, `-k gross_vs_net`).
- The model can add filters nobody asked for (such as a date range). The footer
  always shows the filters that were applied.
- `search_catalog` is keyword-based; it is small enough not to need embeddings yet.
- Personal data is masked by column name (see **Privacy**), which is a real mitigation but not
  a guarantee: an aliased column (`SELECT email AS contact`) is not recognised. A closed-vocabulary,
  aggregate-only path — the separate dataveil package — would close this properly; not integrated yet.
- DuckDB's read-only guarantee is weaker than Postgres's: sqlguard only, no session-level enforcement
  (see `datasources/duckdb_source.py`). Trino is unverified — no cluster in this environment.
- `COPILOT_ALLOW_RUN_SQL`/`COPILOT_MASK_PII`/`COPILOT_API_TOKEN`/the UI login are read once, at
  process start (like `COPILOT_DATASOURCE`); changing them means restarting the process, not a
  live toggle.
- `derive`'s period-over-period trusts the row order it is handed: correct when the rows came
  from query_metric's own time group_by (chronological by default) or an ORDER BY the model wrote
  into run_sql, wrong if some future caller passes unordered rows. It does not re-sort, so it does
  not have to guess which column is "time" for a breakdown that is not a time series at all.
