"""COPILOT_ALLOW_RUN_SQL=false must remove ad-hoc SQL everywhere it could appear: the tool
itself, the tool list handed to the model, and the prompt's own wording. No LLM, no database."""
import importlib
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent  # noqa: E402
import settings  # noqa: E402
import tools  # noqa: E402


def test_the_tool_itself_refuses_when_disabled():
    with mock.patch.object(settings, "ALLOW_RUN_SQL", False):
        result = tools.tool_run_sql("select 1")
        assert "error" in result and "disabled" in result["error"].lower()


def test_prompt_drops_run_sql_and_says_so_when_disabled():
    with mock.patch.object(settings, "ALLOW_RUN_SQL", True):
        assert "run_sql(sql)" in agent.system_prompt()
    with mock.patch.object(settings, "ALLOW_RUN_SQL", False):
        prompt = agent.system_prompt()
        assert "run_sql(sql)" not in prompt
        assert "no ad-hoc sql tool" in prompt.lower()


def test_the_tool_list_and_impls_omit_run_sql_when_disabled():
    try:
        with mock.patch.object(settings, "ALLOW_RUN_SQL", False):
            importlib.reload(tools)
            names = {t["function"]["name"] for t in tools.TOOL_SPECS}
            assert "run_sql" not in names, names
            assert "run_sql" not in tools.TOOL_IMPLS
            assert {"describe_metrics", "query_metric", "search_catalog"} <= names
    finally:
        importlib.reload(tools)  # restore the real (enabled) tool list for every other test


def test_reloading_with_it_enabled_restores_run_sql():
    names = {t["function"]["name"] for t in tools.TOOL_SPECS}
    assert "run_sql" in names and "run_sql" in tools.TOOL_IMPLS
