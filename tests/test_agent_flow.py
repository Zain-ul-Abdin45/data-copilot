"""Agent control flow with the model and the tools stubbed out: no LLM, no database."""
import json
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent  # noqa: E402
import audit  # noqa: E402
import settings  # noqa: E402


def call(name, **args):
    return {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": name, "arguments": args}}]}


def say(text):
    return {"role": "assistant", "content": text}


MONTHS = {"governed": True, "columns": ["metric_time__month", "net_revenue"],
          "rows": [["2026-01-01", 2820.8], ["2026-02-01", 2396.41]], "row_count": 2, "_sql": "sql"}
GROSS = {"governed": True, "columns": ["gross_revenue"], "rows": [[22633.57]], "row_count": 1}
SQL = {"governed": False, "columns": ["m", "v"], "rows": [["a", 1.5], ["b", 2.5]], "row_count": 2,
       "_sql": "SELECT m, v FROM fct_orders LIMIT 200", "_tables": ["fct_orders"]}


def run(script, *, escalate=True, think=False, results=None):
    """Run ask() against a scripted model. Returns (result, seen_calls, audit_rows)."""
    seen = []
    it = iter(script)
    results = results or {"query_metric": GROSS, "run_sql": SQL}

    def fake_chat(messages, tools=None, think=None):
        seen.append({"think": think, "last": messages[-1]})
        return next(it)

    impls = {k: (lambda r: (lambda **kw: dict(r)))(v) for k, v in results.items()}
    with tempfile.TemporaryDirectory() as d, \
            mock.patch.object(agent, "chat", fake_chat), \
            mock.patch.object(agent, "TOOL_IMPLS", impls), \
            mock.patch.object(agent.semantic, "metric_summary", lambda n: f"definition of {n}"), \
            mock.patch.object(agent.semantic, "ratio_metric_names", lambda: set()), \
            mock.patch.object(settings, "AUDIT_PATH", Path(d) / "audit.jsonl"), \
            mock.patch.object(settings, "THINK", think), \
            mock.patch.object(settings, "ESCALATE", escalate):
        out = agent.ask("How is net revenue by month?")
        return out, seen, audit.read()


def test_governed_answer_stays_fast_and_gets_a_code_drawn_table():
    out, seen, _ = run([call("query_metric", metrics=["net_revenue"]), say("Net revenue peaked in February.")],
                       results={"query_metric": MONTHS})
    assert [s["think"] for s in seen] == [False, False] and out["escalated"] is False
    assert "| metric_time__month | net_revenue |" in out["answer"] and "2,396.41" in out["answer"]
    assert "Governed metric: net_revenue (definition of net_revenue)" in out["answer"]


def test_adhoc_sql_escalates_to_thinking_once():
    script = [call("run_sql", sql="x"), say("Done."), call("run_sql", sql="x"), say("Done, carefully.")]
    out, seen, rows = run(script)
    assert [s["think"] for s in seen] == [False, False, True, True]
    assert out["escalated"] and out["escalation_reason"] == "used ad-hoc SQL"
    assert out["llm_answer"] == "Done, carefully." and "Not a governed metric" in out["answer"]
    assert rows[0]["escalated"] is True and rows[0]["steps"][0]["tables"] == ["fct_orders"]


def test_no_escalation_when_disabled():
    out, seen, _ = run([call("run_sql", sql="x"), say("Done.")], escalate=False)
    assert len(seen) == 2 and out["escalated"] is False


def test_invented_figure_gets_one_correction():
    script = [call("query_metric", metrics=["gross_revenue"]), say("Refunded: $1,885.63."),
              say("Gross revenue is 22,633.57; the refunded amount is not available.")]
    out, seen, _ = run(script)
    assert "not in any tool result: $1,885.63" in seen[2]["last"]["content"]
    assert out["ungrounded"] == [] and "Warning" not in out["answer"] and out["escalated"] is False


def test_uncorrected_figure_is_flagged_in_the_answer():
    script = [call("query_metric", metrics=["gross_revenue"]), say("Refunded: $1,885.63."), say("Still $1,885.63.")]
    out, _, rows = run(script, escalate=False)
    assert out["ungrounded"] == ["$1,885.63"]
    assert "Warning: these figures are not in the query results" in out["answer"]
    assert rows[0]["ungrounded"] == ["$1,885.63"]


def test_ending_on_a_tool_error_escalates_and_audit_holds_no_rows():
    script = [call("query_metric", metrics=["x"]), say("Cannot."), call("query_metric", metrics=["x"]), say("Cannot.")]
    out, seen, _ = run(script, results={"query_metric": {"error": "unknown metric"}})
    assert out["escalation_reason"] == "ended on a tool error" and len(seen) == 4
    out, _, rows = run([call("run_sql", sql="x"), say("ok"), call("run_sql", sql="x"), say("ok")])
    assert "1.5" not in json.dumps(rows) and "SELECT m, v" in json.dumps(rows)


def test_follow_up_turns_reach_the_model_and_their_figures_count_as_grounded():
    """History goes in as earlier turns; a figure stated (and grounded) earlier may be repeated."""
    history = [{"question": "What is our net revenue?", "answer": "Net revenue is 20,486.14."}]
    seen = []
    script = iter([call("query_metric", metrics=["gross_revenue"]),
                   say("Gross revenue is 22,633.57, against 20,486.14 net earlier.")])

    def fake_chat(messages, tools=None, think=None):
        seen.append(list(messages))
        return next(script)

    with tempfile.TemporaryDirectory() as d, \
            mock.patch.object(agent, "chat", fake_chat), \
            mock.patch.object(agent, "TOOL_IMPLS", {"query_metric": lambda **kw: dict(GROSS)}), \
            mock.patch.object(agent.semantic, "metric_summary", lambda n: n), \
            mock.patch.object(agent.semantic, "ratio_metric_names", lambda: set()), \
            mock.patch.object(settings, "AUDIT_PATH", Path(d) / "a.jsonl"), \
            mock.patch.object(settings, "THINK", False), mock.patch.object(settings, "ESCALATE", False):
        out = agent.ask("And gross revenue?", history)
        logged = audit.read()
    roles = [(m["role"], m["content"]) for m in seen[0]]
    assert roles[1] == ("user", "What is our net revenue?") and roles[2] == ("assistant", "Net revenue is 20,486.14.")
    assert roles[3] == ("user", "And gross revenue?")
    assert out["ungrounded"] == [], out["ungrounded"]
    assert logged[0]["turn"] == 2
    assert out["tables_md"] == "" and "Governed metric" in out["footer_md"] and out["ratio_metrics"] == []


def test_derive_receives_the_trace_but_the_model_cannot_supply_it():
    """agent.py injects trace into the derive call; it is not a parameter the model controls."""
    seen_kwargs = {}

    def fake_derive(**kw):
        seen_kwargs.update(kw)
        return {"columns": ["metric_time__month", "net_revenue", "net_revenue_change_pct"],
                "rows": [["2026-01-01", 2820.8, None], ["2026-02-01", 2396.41, -0.15045022688598977]],
                "row_count": 2, "governed": True, "_derived_from": "period_over_period_pct"}

    script = [call("query_metric", metrics=["net_revenue"], group_by=["metric_time__month"]),
             call("derive", operation="period_over_period_pct"),
             say("February fell 15.0% from January.")]
    impls = {"query_metric": lambda **kw: dict(MONTHS), "derive": fake_derive}
    with tempfile.TemporaryDirectory() as d, \
            mock.patch.object(agent, "chat", lambda messages, tools=None, think=None: script.pop(0)), \
            mock.patch.object(agent, "TOOL_IMPLS", impls), \
            mock.patch.object(agent.semantic, "ratio_metric_names", lambda: set()), \
            mock.patch.object(agent.semantic, "metric_summary", lambda name: name), \
            mock.patch.object(settings, "AUDIT_PATH", Path(d) / "a.jsonl"), \
            mock.patch.object(settings, "THINK", False), mock.patch.object(settings, "ESCALATE", False):
        out = agent.ask("and month on month?")
    assert "trace" in seen_kwargs and seen_kwargs["trace"][0]["tool"] == "query_metric"
    assert "operation" in seen_kwargs and "trace" not in call("derive", operation="x")["tool_calls"][0]["function"]["arguments"]
    # the derived percent column renders as a percentage and is grounded (166%-style values too)
    assert "-15.0%" in out["tables_md"]
    assert out["ungrounded"] == []


def test_a_change_in_an_already_percentage_column_renders_as_percentage_points():
    ratio_trace = [{"tool": "query_metric", "args": {}, "result": {"columns": ["refund_rate"], "rows": [[0.1]]}},
                   {"tool": "derive", "args": {}, "result": {
                       "columns": ["metric_time__month", "refund_rate", "refund_rate_change"],
                       "rows": [["2026-01-01", 0.0], ["2026-02-01", 0.154]]}}]
    with mock.patch.object(agent.semantic, "ratio_metric_names", lambda: {"refund_rate"}):
        ratio = agent._ratio_metrics(ratio_trace)
    assert ratio == {"refund_rate", "refund_rate_change"}
    # a plain (non-percentage) column's _change must not be swept in
    plain_trace = [{"tool": "query_metric", "args": {}, "result": {"columns": ["net_revenue"], "rows": [[1]]}},
                  {"tool": "derive", "args": {}, "result": {
                      "columns": ["metric_time__month", "net_revenue", "net_revenue_change"],
                      "rows": [["2026-01-01", 0.0], ["2026-02-01", 100.0]]}}]
    with mock.patch.object(agent.semantic, "ratio_metric_names", lambda: {"refund_rate"}):
        assert agent._ratio_metrics(plain_trace) == {"refund_rate"}


def test_focus_tables_are_injected_into_search_catalog_and_named_in_the_prompt():
    seen_kwargs = {}

    def fake_search(**kw):
        seen_kwargs.update(kw)
        return {"matches": []}

    script = [call("search_catalog", query="revenue"), say("Not found.")]
    impls = {"search_catalog": fake_search}
    seen_messages = []

    def fake_chat(messages, tools=None, think=None):
        seen_messages.append(messages[0]["content"])
        return script.pop(0)

    with tempfile.TemporaryDirectory() as d, \
            mock.patch.object(agent, "chat", fake_chat), \
            mock.patch.object(agent, "TOOL_IMPLS", impls), \
            mock.patch.object(agent.semantic, "ratio_metric_names", lambda: set()), \
            mock.patch.object(settings, "AUDIT_PATH", Path(d) / "a.jsonl"), \
            mock.patch.object(settings, "THINK", False), mock.patch.object(settings, "ESCALATE", False):
        agent.ask("what about revenue?", focus_tables={"fct_orders"})
    assert seen_kwargs == {"query": "revenue", "focus": {"fct_orders"}}
    assert "focused this conversation on: fct_orders" in seen_messages[0]


def test_a_stringified_list_argument_is_coerced_before_reaching_the_tool():
    # found via a real llama3.1 run: it filled an array-typed argument with a STRING containing a
    # list literal ("['net_revenue']") instead of a real JSON array. list("['net_revenue']") does
    # not raise, it silently produces individual characters, so this must be caught before the
    # tool ever sees it, not left to surface as a mystifying "no such metric" error downstream.
    seen_kwargs = {}

    def fake_query_metric(**kw):
        seen_kwargs.update(kw)
        return dict(MONTHS)

    script = [call("query_metric", metrics="['net_revenue']", group_by="['metric_time__month']"),
             say("Here it is.")]
    impls = {"query_metric": fake_query_metric}
    with tempfile.TemporaryDirectory() as d, \
            mock.patch.object(agent, "chat", lambda messages, tools=None, think=None: script.pop(0)), \
            mock.patch.object(agent, "TOOL_IMPLS", impls), \
            mock.patch.object(agent.semantic, "ratio_metric_names", lambda: set()), \
            mock.patch.object(agent.semantic, "metric_summary", lambda name: name), \
            mock.patch.object(settings, "AUDIT_PATH", Path(d) / "a.jsonl"), \
            mock.patch.object(settings, "THINK", False), mock.patch.object(settings, "ESCALATE", False):
        agent.ask("net revenue by month?")
    assert seen_kwargs == {"metrics": ["net_revenue"], "group_by": ["metric_time__month"]}


def test_a_leaked_tool_call_in_plain_content_is_recovered_not_treated_as_the_final_answer():
    # the exact shape seen live from llama3.1, mid-conversation, after tools had already worked once
    leaked = '{"name": "describe_metrics", "parameters": {"term":"refund"}}'
    assert agent._recover_leaked_tool_call(leaked) == \
        [{"function": {"name": "describe_metrics", "arguments": {"term": "refund"}}}]
    assert agent._recover_leaked_tool_call('  ' + leaked + '  ') is not None  # surrounding whitespace
    assert agent._recover_leaked_tool_call('{"name": "describe_metrics", "arguments": {"term": "x"}}') is not None
    # the actual live shape: real prose, THEN a trailing leaked call — not JSON from character one
    mixed = 'The gross revenue taken is $542,965.95.\n\n' + leaked
    assert agent._recover_leaked_tool_call(mixed) == \
        [{"function": {"name": "describe_metrics", "arguments": {"term": "refund"}}}]


def test_ordinary_prose_is_never_mistaken_for_a_leaked_tool_call():
    for text in ("Net revenue was $480,561.16.", "", "{not valid json", "{}",
                 '{"name": "delete_everything", "parameters": {}}',  # a real JSON object, unknown tool
                 '{"name": "run_sql", "parameters": "not a dict"}',  # args not a dict
                 "The set {1, 2, 3} was used to compute this."):  # a brace pair that is not JSON at all
        assert agent._recover_leaked_tool_call(text) is None, text


def test_a_tool_call_leaked_into_content_actually_runs_instead_of_ending_the_turn():
    # reproduces the live gross_vs_net failure: query_metric succeeds once, then the model's
    # SECOND tool call arrives as raw JSON in content instead of a proper tool_calls entry — the
    # turn must not end there with only half the answer
    leaked_call = say('{"name": "describe_metrics", "parameters": {"term": "refund"}}')
    script = [call("query_metric", metrics=["gross_revenue"]), leaked_call,
             say("Gross revenue was 22,633.57; refunded_revenue is the amount refunded.")]
    impls = {"query_metric": lambda **kw: dict(GROSS),
             "describe_metrics": lambda term=None: {"metrics": [{"name": "refunded_revenue"}]}}
    with tempfile.TemporaryDirectory() as d, \
            mock.patch.object(agent, "chat", lambda messages, tools=None, think=None: script.pop(0)), \
            mock.patch.object(agent, "TOOL_IMPLS", impls), \
            mock.patch.object(agent.semantic, "ratio_metric_names", lambda: set()), \
            mock.patch.object(agent.semantic, "metric_summary", lambda name: name), \
            mock.patch.object(settings, "AUDIT_PATH", Path(d) / "a.jsonl"), \
            mock.patch.object(settings, "THINK", False), mock.patch.object(settings, "ESCALATE", False):
        out = agent.ask("gross revenue and refunds?")
    assert [t["tool"] for t in out["trace"]] == ["query_metric", "describe_metrics"]
    assert out["llm_answer"] == "Gross revenue was 22,633.57; refunded_revenue is the amount refunded."


def test_a_singular_metric_argument_is_renamed_to_the_plural_the_tool_expects():
    # found live: llama3.1 called query_metric with "metric" then "metric_name" instead of the
    # actual parameter "metrics", neither named in query_metric()'s own function name
    assert agent._coerce_metric_arg("query_metric", {"metric": "net_revenue"}) == {"metrics": ["net_revenue"]}
    assert agent._coerce_metric_arg("query_metric", {"metric_name": ["net_revenue"]}) == {"metrics": ["net_revenue"]}
    # "metrics" already given: never overwritten by a stray alias also present
    assert agent._coerce_metric_arg("query_metric", {"metrics": ["a"], "metric": "b"}) == {"metrics": ["a"], "metric": "b"}
    # only query_metric is touched: another tool's own "metric"-shaped argument is left alone
    assert agent._coerce_metric_arg("describe_metrics", {"metric": "x"}) == {"metric": "x"}


def test_a_bad_argument_error_names_the_real_parameters_so_a_model_can_actually_correct_itself():
    # a wrong argument name outside _coerce_metric_arg's aliases (so the TypeError path itself,
    # not the alias shortcut, is what's under test here) must come back naming what IS valid, not
    # just what was wrong — that is the actual fix: a model told only "topic is wrong" has nothing
    # to converge on, but told "the parameters are exactly: term" does
    script = [call("describe_metrics", topic="refunds"), say("Done.")]
    impls = {"describe_metrics": lambda term=None: {"metrics": []}}  # a real signature, so an
    # unexpected kwarg actually raises TypeError instead of being silently absorbed by **kw
    with tempfile.TemporaryDirectory() as d, \
            mock.patch.object(agent, "chat", lambda messages, tools=None, think=None: script.pop(0)), \
            mock.patch.object(agent, "TOOL_IMPLS", impls), \
            mock.patch.object(agent.semantic, "ratio_metric_names", lambda: set()), \
            mock.patch.object(settings, "AUDIT_PATH", Path(d) / "a.jsonl"), \
            mock.patch.object(settings, "THINK", False), mock.patch.object(settings, "ESCALATE", False):
        out = agent.ask("what is a refund?")
    err = out["trace"][0]["result"]
    assert "topic" in err["error"] and "Bad arguments" in err["error"]
    assert err["hint"] == "describe_metrics's parameters are exactly: term."


def test_bad_arguments_hint_lists_the_tools_own_parameter_names():
    # exercised directly: agent._TOOL_PARAM_NAMES is built from TOOL_SPECS, not TOOL_IMPLS, so an
    # injected argument (trace, focus) never appears as something the model could pass itself
    assert agent._TOOL_PARAM_NAMES["query_metric"] == \
        sorted(["metrics", "group_by", "filters", "start_date", "end_date", "order_by", "limit"])
    assert "trace" not in agent._TOOL_PARAM_NAMES["derive"] and "operation" in agent._TOOL_PARAM_NAMES["derive"]
    assert "focus" not in agent._TOOL_PARAM_NAMES["search_catalog"]


def test_coerce_stringified_lists_leaves_ordinary_arguments_alone():
    args = {"metrics": ["net_revenue"], "term": "net revenue", "limit": 3, "sql": "select 1",
            "filters": [{"dimension": "order__status", "operator": "=", "value": "refunded"}]}
    assert agent._coerce_stringified_lists(args) == args
    # a string that merely starts and ends with brackets but isn't a real list is passed through
    assert agent._coerce_stringified_lists({"sql": "[not a list, just SQL-ish text]"}) == \
        {"sql": "[not a list, just SQL-ish text]"}


def test_no_focus_tables_means_no_mention_and_none_passed():
    def fake_search(**kw):
        assert kw.get("focus") is None
        return {"matches": []}

    script = [call("search_catalog", query="x"), say("ok")]
    seen_messages = []

    def fake_chat(messages, tools=None, think=None):
        seen_messages.append(messages[0]["content"])
        return script.pop(0)

    with tempfile.TemporaryDirectory() as d, \
            mock.patch.object(agent, "chat", fake_chat), \
            mock.patch.object(agent, "TOOL_IMPLS", {"search_catalog": fake_search}), \
            mock.patch.object(agent.semantic, "ratio_metric_names", lambda: set()), \
            mock.patch.object(settings, "AUDIT_PATH", Path(d) / "a.jsonl"), \
            mock.patch.object(settings, "THINK", False), mock.patch.object(settings, "ESCALATE", False):
        agent.ask("x?")
    assert "focused this conversation on" not in seen_messages[0]
