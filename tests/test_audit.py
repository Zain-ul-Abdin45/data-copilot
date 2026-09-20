"""Audit log. No LLM, no database."""
import json
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import audit  # noqa: E402
import settings  # noqa: E402

TRACE = [{"tool": "run_sql", "args": {"sql": "select 1"},
          "result": {"columns": ["email"], "rows": [["secret@example.com"]], "row_count": 1,
                     "_sql": "SELECT 1 LIMIT 200", "_tables": ["dim_customers"]}},
         {"tool": "query_metric", "args": {}, "result": {"error": "bad"}}]


def test_summary_never_contains_rows():
    text = json.dumps(audit.summarize_trace(TRACE))
    assert "secret@example.com" not in text
    assert "SELECT 1 LIMIT 200" in text and '"row_count": 1' in text and '"ok": false' in text


def test_log_appends_lines():
    with tempfile.TemporaryDirectory() as d, mock.patch.object(settings, "AUDIT_PATH", Path(d) / "x" / "a.jsonl"):
        audit.log({"question": "q1", "steps": []})
        audit.log({"question": "q2", "steps": []})
        rows = audit.read()
        assert [r["question"] for r in rows] == ["q1", "q2"] and "ts" in rows[0]


def test_log_failure_does_not_raise():
    with tempfile.NamedTemporaryFile() as f, mock.patch.object(settings, "AUDIT_PATH", Path(f.name) / "a.jsonl"):
        audit.log({"question": "q", "steps": []})  # parent is a file: must be swallowed
