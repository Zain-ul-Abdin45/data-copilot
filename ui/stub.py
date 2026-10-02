"""A stand-in for the agent, so the interface can be started and tested without a model.
Selected with COPILOT_UI_STUB=1. It returns canned answers shaped exactly like agent.ask().

Covers the two kinds of question this product answers, not just metrics: a governed metric
(the default canned answer) and a schema/structure question answered from the catalog instead
of a query (a few keywords below route to it). The badge shown ("Governed metric" vs. "From
the data catalog") is decided by ui/render.py's classify() purely from what tool produced the
trace, the same as it would for the real agent — so this is the stub demonstrating an existing
capability, not special-cased UI behaviour that only exists here.
"""

MONTHS = [["2026-01-01", 2820.8, 0.0], ["2026-02-01", 2396.41, 0.1538], ["2026-03-01", 1856.89, 0.0625],
          ["2026-04-01", 3040.45, 0.0], ["2026-05-01", 2261.63, 0.0], ["2026-06-01", 3473.4, 0.1]]

# Real column metadata from dbt-test-project/models/marts/schema.yml, so this demonstrates the
# actual PII-masking behaviour (README's Privacy section) rather than inventing columns.
_TABLES = {
    "dim_customers": {
        "layer": "marts", "description": "One row per customer with lifetime order count.",
        "columns": [
            ("customer_id", "integer", None, False),
            ("first_name", "character varying", None, True),
            ("last_name", "character varying", None, True),
            ("email", "character varying", None, True),
            ("lifetime_orders", "bigint", None, False),
            ("first_order_date", "date",
             "The date of this customer's earliest order, null if they have none.", False),
        ],
    },
    "fct_orders": {
        "layer": "marts",
        "description": ("One row per order with the total amount paid and the business-rule "
                         "flags from order_status_rules."),
        "columns": [
            ("order_id", "integer", None, False),
            ("customer_id", "integer", None, False),
            ("order_date", "date", None, False),
            ("status", "character varying", None, False),
            ("counts_in_gross_revenue", "boolean",
             "Business rule; payments on this order count as gross revenue.", False),
            ("is_refund", "boolean",
             "Business rule; payments on this order are subtracted to get net revenue.", False),
            ("counts_as_net_order", "boolean",
             "Business rule; this order counts as a net order.", False),
            ("order_total", "numeric", None, False),
        ],
    },
}
_STRUCTURE_WORDS = ("structure", "column", "columns", "schema", "field", "fields")


def _catalog_answer(question: str, turn: int) -> dict | None:
    q = question.lower()
    table = next((t for t in _TABLES if t in q), None)
    if table is None or not any(w in q for w in _STRUCTURE_WORDS):
        return None
    meta = _TABLES[table]
    columns = [{"name": c, "type": t, **({"description": d} if d else {}),
                **({"pii": True, "note": "personal data: hidden in every tool result, not just here"}
                   if pii else {})}
               for c, t, d, pii in meta["columns"]]
    match = {"name": table, "kind": "table", "layer": meta["layer"], "description": meta["description"],
             "queryable_with_run_sql": True, "columns": columns}
    trace = [{"tool": "search_catalog", "args": {"query": question}, "result": {"matches": [match]}}]
    shown = ", ".join(c["name"] + (" [personal data hidden]" if c.get("pii") else f" ({c['type']})")
                       for c in columns)
    text = f"(stub, turn {turn}) {table} ({meta['layer']}): {meta['description']} Columns: {shown}."
    return {"answer": text, "llm_answer": text, "trace": trace, "ungrounded": [], "step_limit": False,
            "think": False, "escalated": False, "elapsed_s": 0.1, "wall_s": 0.1, "tables_md": "",
            "footer_md": "\n\n---\nHow this was calculated\n- Looked up in the data catalog (stub)",
            "ratio_metrics": []}


def ask(question: str, history: list[dict] | None = None, focus_tables: set[str] | None = None) -> dict:
    turn = len(history or []) + 1
    result = _catalog_answer(question, turn)
    if result is None:
        trace = [{"tool": "query_metric", "args": {"metrics": ["net_revenue", "refund_rate"],
                                                    "group_by": ["metric_time__month"]},
                  "result": {"governed": True, "columns": ["metric_time__month", "net_revenue", "refund_rate"],
                             "rows": MONTHS, "row_count": len(MONTHS), "_sql": "SELECT 1 /* stub */"}}]
        text = f"(stub, turn {turn}) Net revenue peaked in June at 3,473.40."
        result = {"answer": text, "llm_answer": text, "trace": trace, "ungrounded": [], "step_limit": False,
                  "think": False, "escalated": False, "elapsed_s": 0.1, "wall_s": 0.1,
                  "tables_md": "| month | net revenue |\n| --- | ---: |\n| Jan 2026 | 2,820.80 |",
                  "footer_md": "\n\n---\nHow this was calculated\n- Governed metric: net_revenue (stub)",
                  "ratio_metrics": ["refund_rate"]}
    if focus_tables:
        suffix = f" (focused on: {', '.join(sorted(focus_tables))})"
        result["answer"] += suffix
        result["llm_answer"] += suffix
    return result
