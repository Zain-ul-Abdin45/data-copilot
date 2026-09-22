"""
The four capabilities the agent has. None can change data: the metric tool
compiles SQL from governed definitions, run_sql is parsed and restricted by
sqlguard, and both run as a database role that cannot write. Keys starting
with "_" in a result are kept for the audit trail and never shown to the model.
"""
import datasources
import db
import derive
import privacy
import semantic
import settings
import sqlguard
from catalog import relations, search_catalog


def _mask(result: dict) -> dict:
    """Applied to every result that carries rows, from whichever tool produced them."""
    if "rows" not in result:
        return result
    rows, masked_cols = privacy.mask_rows(result["columns"], result["rows"], settings.MASK_PII)
    result["rows"] = rows
    if masked_cols:
        result["masked_columns"] = masked_cols
        result["note"] = (result.get("note", "") + " " if result.get("note") else "") + (
            f"Personal data ({', '.join(masked_cols)}) is hidden; refer to customers by "
            "customer_id, not by name or email.")
    return result


def tool_describe_metrics(term: str = "") -> dict:
    return semantic.describe_metrics(term or None)


def tool_query_metric(metrics: list[str], group_by: list[str] | None = None,
                      filters: list[dict] | None = None, start_date: str | None = None,
                      end_date: str | None = None, order_by: list[str] | None = None,
                      limit: int | None = None) -> dict:
    return _mask(semantic.query_metric(metrics, group_by, filters, start_date, end_date, order_by, limit))


def tool_search_catalog(query: str) -> dict:
    return search_catalog(query)


def tool_derive(operation: str, columns: list[str] | None = None, trace: list[dict] | None = None) -> dict:
    """Computed in code over the most recent query_metric/run_sql result in this conversation,
    never by the model doing the arithmetic itself. `trace` is supplied by the agent loop, not
    the model: it is not part of the tool's public parameters (see TOOL_SPECS)."""
    base = derive.last_result(trace or [])
    if base is None:
        return {"error": "No prior result with two or more rows to compute this from. Call "
                         "query_metric or run_sql first, then derive from it."}
    try:
        out = derive.apply(operation, base, columns)
    except ValueError as e:
        return {"error": str(e)}
    return _mask({**out, "governed": base.get("governed", False), "_derived_from": operation})


def tool_run_sql(sql: str) -> dict:
    if not settings.ALLOW_RUN_SQL:
        return {"error": "Ad-hoc SQL is disabled on this deployment. Only governed metrics "
                         "(query_metric) and catalog metadata (search_catalog) are answerable; "
                         "if neither covers this question, say plainly that it cannot be "
                         "answered from what is available, without guessing at numbers."}
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
    return _mask({"governed": False, "columns": cols, "rows": rows, "row_count": len(rows),
                  "_sql": safe_sql, "_tables": tables})


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
        "name": "derive",
        "description": (
            "Compute a transform of the most recent query_metric or run_sql result in this "
            "conversation (its rows, not a fresh query): period_over_period_pct (percentage "
            "change vs. the previous row, e.g. month on month if the rows are ordered by month), "
            "period_over_period_change (the same, as an amount, not a percentage), or "
            "share_of_total (each row's value as a percentage of the column's sum, e.g. each "
            "payment method's share of total revenue). Use this instead of computing a change or "
            "a share yourself, and instead of a governed metric for a growth rate that has "
            "described_metrics has none defined for. columns picks which numeric column(s); "
            "default is every numeric column. If a named governed metric already covers this "
            "(check describe_metrics first, e.g. net_revenue has net_revenue_growth_mom), prefer "
            "that: it is exact, this is a general-purpose fallback for everything else."),
        "parameters": {"type": "object", "properties": {
            "operation": {"type": "string", "enum": list(derive.OPERATIONS)},
            "columns": {"type": "array", "items": {"type": "string"}}},
            "required": ["operation"]}}},
]

TOOL_IMPLS = {
    "describe_metrics": tool_describe_metrics,
    "query_metric": tool_query_metric,
    "search_catalog": tool_search_catalog,
    "derive": tool_derive,
}

if settings.ALLOW_RUN_SQL:
    TOOL_SPECS.append({"type": "function", "function": {
        "name": "run_sql",
        "description": (
            "Fallback when no governed metric or dimension can answer. Runs one read-only "
            f"{datasources.get().dialect} SELECT over the {datasources.get().schema} schema "
            f"(max {settings.ROW_LIMIT} rows). Results are NOT governed metrics and are labelled "
            "as such. For money, join order_status_rules on status and use its flags instead of "
            "hard-coding statuses."),
        "parameters": {"type": "object", "properties": {"sql": {"type": "string"}},
                       "required": ["sql"]}}})
    TOOL_IMPLS["run_sql"] = tool_run_sql
