"""One place for paths and connection settings. Everything is overridable by
environment variable so the same code runs against a different database."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DBT_DIR = Path(os.getenv("DBT_PROJECT_DIR", ROOT.parent / "dbt-test-project"))

# Which engine run_sql, the catalog and (via DBT_TARGET below) the governed path all point at.
# Each is a separate dbt profile target in dbt-test-project/profiles.yml; see datasources/.
DATASOURCE = os.getenv("COPILOT_DATASOURCE", "postgres")  # postgres | duckdb | trino
DBT_TARGET = os.getenv("DBT_TARGET", DATASOURCE)

# --- postgres (COPILOT_DATASOURCE=postgres) -----------------------------------------------
# The agent only ever connects as a read-only role that can see the analytics
# schema and nothing else (see the grants in the README).
DB_HOST = os.getenv("COPILOT_DB_HOST", "localhost")
DB_NAME = os.getenv("COPILOT_DB_NAME", "data_copilot")
DB_USER = os.getenv("COPILOT_DB_USER", "copilot_ro")
DB_PASSWORD = os.getenv("COPILOT_DB_PASSWORD", "copilot_ro")
SCHEMA = "analytics"

# --- duckdb (COPILOT_DATASOURCE=duckdb): a local file, no server, no login ------------------
DUCKDB_PATH = os.getenv("COPILOT_DUCKDB_PATH", str(ROOT / "warehouse.duckdb"))
DUCKDB_SCHEMA = os.getenv("COPILOT_DUCKDB_SCHEMA", "analytics")

# --- trino (COPILOT_DATASOURCE=trino): verified against a real cluster, see README / trino/ --
TRINO_HOST = os.getenv("COPILOT_TRINO_HOST", "localhost")
TRINO_PORT = int(os.getenv("COPILOT_TRINO_PORT", "8080"))
TRINO_USER = os.getenv("COPILOT_TRINO_USER", "copilot_ro")
TRINO_CATALOG = os.getenv("COPILOT_TRINO_CATALOG", "warehouse")
TRINO_SCHEMA = os.getenv("COPILOT_TRINO_SCHEMA", "analytics")

ROW_LIMIT = 200
STATEMENT_TIMEOUT_MS = 15_000  # postgres only; see datasources/duckdb_source.py's known gap

# Personal data (email, name, ...) is masked out of every tool result before it reaches the
# model or the interface. Off only makes sense on a warehouse with no personal data at all.
MASK_PII = os.getenv("COPILOT_MASK_PII", "true").lower() == "true"

# Whether the model may fall back to ad-hoc SQL at all. Off means only governed metrics and
# catalog metadata are answerable; a question needing something no metric covers gets an
# honest "cannot answer that" instead of arbitrary SQL. See agent.py and tools.py.
ALLOW_RUN_SQL = os.getenv("COPILOT_ALLOW_RUN_SQL", "true").lower() == "true"

# A shared bearer token for the /ask API (main.py). Empty (the default) means no auth, matching
# the current "localhost only, no login" trust model; set it before exposing the API elsewhere.
API_TOKEN = os.getenv("COPILOT_API_TOKEN", "")

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434/api/chat")
MODEL = os.getenv("COPILOT_MODEL", "qwen3:14b")
EMBED_MODEL = os.getenv("COPILOT_EMBED_MODEL", "nomic-embed-text")
# search_catalog's keyword overlap alone decides WHICH entries match at all; this only re-ranks
# among entries that already matched (see catalog._ranked). A raw cosine threshold was tried and
# measured, live, not to separate an unrelated query from a real one cleanly enough on a catalog
# this small to trust it for deciding a match on its own — see catalog.py's docstring.
CATALOG_EMBEDDINGS = os.getenv("COPILOT_CATALOG_EMBEDDINGS", "false").lower() == "true"
# Fast first: thinking is off by default (~15 s/question). Answers that used ad-hoc SQL, ended
# in an error, or contain unverified figures are re-run once with thinking on (~3 min).
THINK = os.getenv("COPILOT_THINK", "false").lower() == "true"
ESCALATE = os.getenv("COPILOT_ESCALATE", "true").lower() == "true"
NUM_CTX = int(os.getenv("COPILOT_NUM_CTX", "16384"))

# One JSON line per question: tools, SQL, row counts. Never result rows.
AUDIT_PATH = Path(os.getenv("COPILOT_AUDIT_PATH", ROOT / "logs" / "audit.jsonl"))
