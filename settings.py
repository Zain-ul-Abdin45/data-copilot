"""One place for paths and connection settings. Everything is overridable by
environment variable so the same code runs against a different database."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DBT_DIR = Path(os.getenv("DBT_PROJECT_DIR", ROOT.parent / "dbt-test-project"))

# The agent only ever connects as a read-only role that can see the analytics
# schema and nothing else (see the grants in the README).
DB_HOST = os.getenv("COPILOT_DB_HOST", "localhost")
DB_NAME = os.getenv("COPILOT_DB_NAME", "data_copilot")
DB_USER = os.getenv("COPILOT_DB_USER", "copilot_ro")
DB_PASSWORD = os.getenv("COPILOT_DB_PASSWORD", "copilot_ro")
SCHEMA = "analytics"

ROW_LIMIT = 200
STATEMENT_TIMEOUT_MS = 15_000

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434/api/chat")
MODEL = os.getenv("COPILOT_MODEL", "qwen3:14b")
# Fast first: thinking is off by default (~15 s/question). Answers that used ad-hoc SQL, ended
# in an error, or contain unverified figures are re-run once with thinking on (~3 min).
THINK = os.getenv("COPILOT_THINK", "false").lower() == "true"
ESCALATE = os.getenv("COPILOT_ESCALATE", "true").lower() == "true"
NUM_CTX = int(os.getenv("COPILOT_NUM_CTX", "16384"))

# One JSON line per question: tools, SQL, row counts. Never result rows.
AUDIT_PATH = Path(os.getenv("COPILOT_AUDIT_PATH", ROOT / "logs" / "audit.jsonl"))
