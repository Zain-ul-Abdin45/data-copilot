"""
The four capabilities the agent has. None can change data: the metric tool
compiles SQL from governed definitions, run_sql is parsed and restricted by
sqlguard, and both run as a database role that cannot write. Keys starting
with "_" in a result are kept for the audit trail and never shown to the model.
"""
import db
import semantic
import settings
import sqlguard
from catalog import relations, search_catalog


def tool_describe_metrics(term: str = "") -> dict:
    return semantic.describe_metrics(term or None)


def tool_query_metric(metrics: list[str], group_by: list[str] | None = None,
                      filters: list[dict] | None = None, start_date: str | None = None,
                      end_date: str | None = None, order_by: list[str] | None = None,
                      limit: int | None = None) -> dict:
    return semantic.query_metric(metrics, group_by, filters, start_date, end_date, order_by, limit)


def tool_search_catalog(query: str) -> dict:
    return search_catalog(query)


def tool_run_sql(sql: str) -> dict:
    try:
        safe_sql, tables = sqlguard.validate(sql, relations())
    except sqlguard.SqlRejected as e:
        return {"error": str(e)}
    try:
        cols, rows = db.query(safe_sql)
    except Exception as e:
        # Show the real columns of the tables used, so a wrong column or join can be fixed
        # in one retry instead of the model giving up.
        known = {m["name"]: [c["name"] for c in m["columns"]]
                 for m in search_catalog(" ".join(tables))["matches"] if m["name"] in tables}
        return {"error": " ".join(str(e).split())[:400],
                "columns_of_tables_used": known,
                "hint": "Fix the query using these real column names and call run_sql again."}
    return {"governed": False, "columns": cols, "rows": rows, "row_count": len(rows),
            "_sql": safe_sql, "_tables": tables}


TOOL_SPECS = [
    {"type": "function", "function": {
        "name": "describe_metrics",
        "description": (
            "Governed business metrics and the business rules behind them. Call with a business "
            "word (revenue, AOV, refund rate) to resolve it to a metric and get its definition, "
            "valid group_by names and the rules. Call with no term to list every metric. Use this "
            "before query_metric and for 'how is X calculated' questions."),
        "parameters": {"type": "object", "properties": {"term": {"type": "string"}}}}},
    {"type": "function", "function": {
        "name": "query_metric",
        "description": (
            "Get numbers for governed metrics. Preferred for any question about a metric. Never "
            "write SQL for these. group_by names look like order__status or metric_time__month. "
            "filters is a list like {\"dimension\": \"order__status\", \"operator\": \"=\", "
            "\"value\": \"delivered\"}. Dates are YYYY-MM-DD and inclusive. order_by takes a "
            "metric or group_by name, prefix - for descending."),
        "parameters": {"type": "object", "properties": {
            "metrics": {"type": "array", "items": {"type": "string"}},
            "group_by": {"type": "array", "items": {"type": "string"}},
            "filters": {"type": "array", "items": {"type": "object", "properties": {
                "dimension": {"type": "string"}, "operator": {"type": "string"},
                "value": {}}, "required": ["dimension", "value"]}},
            "start_date": {"type": "string"}, "end_date": {"type": "string"},
            "order_by": {"type": "array", "items": {"type": "string"}},
            "limit": {"type": "integer"}},
            "required": ["metrics"]}}},
    {"type": "function", "function": {
        "name": "search_catalog",
        "description": (
            "Find which tables, columns and metrics exist and what they mean. Use it for 'do we "
            "have / track X' questions (answer from metadata alone, no SQL) and before run_sql to "
            "find the right table. If nothing matches, the data does not exist."),
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}},
                       "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "run_sql",
        "description": (
            "Fallback when no governed metric or dimension can answer. Runs one read-only "
            f"PostgreSQL SELECT over the {settings.SCHEMA} schema (max {settings.ROW_LIMIT} rows). "
            "Results are NOT governed metrics and are labelled as such. For money, join "
            "order_status_rules on status and use its flags instead of hard-coding statuses."),
        "parameters": {"type": "object", "properties": {"sql": {"type": "string"}},
                       "required": ["sql"]}}},
]

TOOL_IMPLS = {
    "describe_metrics": tool_describe_metrics,
    "query_metric": tool_query_metric,
    "search_catalog": tool_search_catalog,
    "run_sql": tool_run_sql,
}
