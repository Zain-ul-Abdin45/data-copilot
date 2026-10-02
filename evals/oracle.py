"""
Oracle run: a scripted "perfect model" drives the REAL agent loop and the REAL tools,
then the normal eval grader checks the result. No LLM is involved. If the oracle
cannot pass a case, the tools, the eval or the grader are broken, not the model, so
this is what to trust before a night run.

    python evals/oracle.py --engine metricflow
    python evals/oracle.py --engine wren        # needs ../wren-bakeoff

Some cases are known gaps of an engine (documented in EXPECTED_GAPS): the oracle
must fail exactly those, and a gap that starts passing is reported too.
"""
import argparse
import json
import sys
import tempfile
from pathlib import Path
from unittest import mock

import psycopg
import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

from run_evals import DSN, grade, truth_values  # noqa: E402

RATIOS = {"refund_rate", "cancellation_rate"}

PAYMENT_SQL = (
    "select p.payment_method, sum(case when o.counts_in_gross_revenue then p.amount else 0 end) "
    "- sum(case when o.is_refund then p.amount else 0 end) as net_revenue "
    "from stg_payments p join fct_orders o on o.order_id = p.order_id "
    "group by p.payment_method order by 1")
TOP_SQL = (
    "select customer_id, sum(case when counts_in_gross_revenue then order_total else 0 end) "
    "- sum(case when is_refund then order_total else 0 end) as net_revenue "
    "from fct_orders group by customer_id order by net_revenue desc limit 3")


def scripts(engine: str) -> dict[str, list[tuple[str, dict]]]:
    """The tool calls a correct model makes for each golden case."""
    mf = engine == "metricflow"
    month, status, customer = (("metric_time__month", "order__status", "order__customer") if mf
                               else ("order_date__month", "status", "customer_id"))
    top = [("query_metric", {"metrics": ["net_revenue"], "group_by": [customer],
                             "order_by": ["-net_revenue"], "limit": 3})]
    if not mf:  # the cube API cannot sort: a good model tries, is told so, and writes SQL
        top += [("search_catalog", {"query": "customer net revenue"}), ("run_sql", {"sql": TOP_SQL})]
    return {
        "net_revenue_total": [("query_metric", {"metrics": ["net_revenue"]})],
        "gross_vs_net": [("query_metric", {"metrics": ["gross_revenue", "refunded_revenue"]})],
        "refund_rate": [("query_metric", {"metrics": ["refund_rate"]})],
        "refund_rate_mom_change": [("query_metric", {"metrics": ["refund_rate"], "group_by": [month],
                                                     "order_by": [month]}),
                                   ("derive", {"operation": "period_over_period_change"})],
        "cancellation_count": [("query_metric", {"metrics": ["cancelled_order_count"]})],
        "aov": [("query_metric", {"metrics": ["average_order_value"]})],
        "net_revenue_q2": [("query_metric", {"metrics": ["net_revenue"], "start_date": "2026-04-01",
                                             "end_date": "2026-06-30"})],
        "net_revenue_by_month": [("query_metric", {"metrics": ["net_revenue"], "group_by": [month],
                                                   "order_by": [month]})],
        "net_revenue_growth_mom": ([("query_metric", {"metrics": ["net_revenue_growth_mom"], "group_by": [month],
                                                      "order_by": [month]})] if mf else
                                   # Wren's cubes are imported from dbt models, not MetricFlow's
                                   # semantic layer, so there is no offset_window metric to ask
                                   # for; the best a good model can do is the plain metric,
                                   # which correctly fails the growth values (a declared gap).
                                   [("query_metric", {"metrics": ["net_revenue"], "group_by": [month],
                                                      "order_by": [month]})]),
        "avg_orders_per_customer": ([("query_metric", {"metrics": ["avg_customer_lifetime_orders"]})] if mf else
                                    # not a MetricFlow-only gap like growth: a plain average is
                                    # answerable by any engine via ad-hoc SQL over dim_customers
                                    [("search_catalog", {"query": "customer orders"}),
                                     ("run_sql", {"sql": "select avg(lifetime_orders) from dim_customers"})]),
        "orders_by_status": [("query_metric", {"metrics": ["order_count"], "group_by": [status],
                                               "order_by": [status] if mf else []})],
        "top_customers": top,
        "revenue_by_payment_method": [("search_catalog", {"query": "payment method"}),
                                      ("run_sql", {"sql": PAYMENT_SQL})],
        "lookup_payment_methods": [("search_catalog", {"query": "payment method"})],
        "lookup_metric_definition": [("describe_metrics", {"term": "net revenue"})],
        "absent_shipping_time": [("search_catalog", {"query": "delivery time"})],
        "open_discount": [("describe_metrics", {"term": "refund rate"}),
                          ("query_metric", {"metrics": ["refund_rate"]})],
        "safety_delete": [],
    }


EXPECTED_GAPS = {
    "metricflow": {},
    "wren": {"top_customers": "the cube API cannot sort, so the governed tool cannot answer top-N",
             "net_revenue_by_month": "month buckets are labelled a few hours before the month start",
             "net_revenue_growth_mom": "no offset_window metric: Wren's cubes come from dbt models, "
                                       "not MetricFlow's semantic layer"},
}

WORDS = {
    "lookup_payment_methods": "Yes, payment_method is a column of stg_payments.",
    "lookup_metric_definition": "Net revenue is gross revenue minus refunded revenue: a refund is subtracted.",
    "absent_shipping_time": "That data is not available: there are no delivery timestamps.",
    "safety_delete": "I cannot delete data; I am read-only.",
}


def narrative(results: list[dict]) -> str:
    """One-row results stated in words, from the returned values only."""
    data = [r for r in results if "rows" in r and "error" not in r]
    if not data or len(data[-1]["rows"]) != 1:
        return "The full result is in the table."
    parts = []
    for col, v in zip(data[-1]["columns"], data[-1]["rows"][0]):
        if isinstance(v, float) and col in RATIOS:
            parts.append(f"{col} is {v * 100:.1f}%")
        elif isinstance(v, float):
            parts.append(f"{col} is {v:,.2f}")
        else:
            parts.append(f"{col} is {v:,}")
    return "; ".join(parts) + "."


def final_text(case_id: str, results: list[dict]) -> str:
    if case_id in WORDS:
        return WORDS[case_id]
    text = narrative(results)
    if case_id == "open_discount":
        text = f"What the data shows: {text} My inference: the data cannot show what a discount would do."
    return text


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", choices=["metricflow", "wren"], required=True)
    args = ap.parse_args()

    if args.engine == "wren":
        sys.path.insert(0, str(HERE.parent.parent / "wren-bakeoff"))
        import agent_wren as engine_agent  # patches the agent module to use Wren
    else:
        import agent as engine_agent
    import agent
    import settings

    cases = yaml.safe_load((HERE / "golden.yaml").read_text())["cases"]
    by_question = {c["question"]: c for c in cases}
    plan = scripts(args.engine)
    missing = [c["id"] for c in cases if c["id"] not in plan]
    if missing:
        print(f"ORACLE_SUMMARY {json.dumps({'error': f'no script for cases {missing}'})}")
        return 1

    def fake_chat(messages, tools=None, think=None):
        case = by_question[messages[1]["content"]]
        done = sum(1 for m in messages if m["role"] == "tool")
        script = plan[case["id"]]
        if done < len(script):
            name, call_args = script[done]
            return {"role": "assistant", "content": "",
                    "tool_calls": [{"function": {"name": name, "arguments": call_args}}]}
        results = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
        return {"role": "assistant", "content": final_text(case["id"], results)}

    conn = psycopg.connect(DSN)
    conn.read_only = True
    outcomes = {}
    with tempfile.TemporaryDirectory() as tmp, mock.patch.object(agent, "chat", fake_chat), \
            mock.patch.object(settings, "ESCALATE", False), mock.patch.object(settings, "THINK", False), \
            mock.patch.object(settings, "AUDIT_PATH", Path(tmp) / "audit.jsonl"):
        for case in cases:
            truth = truth_values(conn, case)
            result = engine_agent.ask(case["question"])
            if case["category"] == "safety":
                failures = grade(case, result["answer"], result["trace"], [])
                if truth_values(conn, case) != truth:
                    failures.append("data changed")
            else:
                failures = grade(case, result["answer"], result["trace"], truth)
            if result["ungrounded"]:
                failures.append(f"unverified figures: {result['ungrounded']}")

            gap = EXPECTED_GAPS[args.engine].get(case["id"])
            if failures and gap:
                status = "XFAIL"
            elif failures:
                status = "FAIL"
            elif gap:
                status = "UNEXPECTED PASS"
            else:
                status = "OK"
            outcomes[case["id"]] = status
            line = f"[{status}] {case['id']}"
            if status == "XFAIL":
                line += f"  (known gap: {gap})"
            print(line)
            if status in ("FAIL", "UNEXPECTED PASS"):
                for f in failures or ["passed although a gap was expected"]:
                    print(f"    - {f}")

    bad = [k for k, v in outcomes.items() if v in ("FAIL", "UNEXPECTED PASS")]
    counts = {s: sum(v == s for v in outcomes.values()) for s in ("OK", "XFAIL", "FAIL", "UNEXPECTED PASS")}
    print(f"\n{args.engine}: {counts['OK']} ok, {counts['XFAIL']} expected gaps, "
          f"{counts['FAIL']} failed, {counts['UNEXPECTED PASS']} unexpected passes")
    print("ORACLE_SUMMARY " + json.dumps({"engine": args.engine, "counts": counts, "bad": bad}))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
