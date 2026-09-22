"""
Deterministic transforms over a result the agent already has: period-over-period change,
share of total. Computed here, in code, over whichever rows query_metric or run_sql actually
returned — never by asking the model to do arithmetic across many rows itself, the same
principle as tables.py's code-rendered tables and the offset_window metrics in the semantic
layer. Where a named governed metric exists for something common (net_revenue_growth_mom), it
stays preferable; this is what covers everything else, without a new metric per question.
"""

OPERATIONS = ("period_over_period_pct", "period_over_period_change", "share_of_total")


def _numeric_columns(columns: list[str], rows: list[list]) -> list[int]:
    idx = []
    for i in range(len(columns)):
        vals = [r[i] for r in rows if r[i] is not None]
        if vals and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in vals):
            idx.append(i)
    return idx


def last_result(trace: list[dict]) -> dict | None:
    """The most recent successful query_metric/run_sql result with 2+ rows to derive from."""
    for step in reversed(trace):
        if step["tool"] not in ("query_metric", "run_sql"):
            continue
        r = step["result"]
        if "error" not in r and len(r.get("rows", [])) >= 2:
            return r
    return None


def _resolve_columns(columns: list[str], rows: list[list], wanted: list[str] | None) -> list[int]:
    if not wanted:
        idx = _numeric_columns(columns, rows)
        if not idx:
            raise ValueError("no numeric column in the result to compute this from")
        return idx
    missing = [c for c in wanted if c not in columns]
    if missing:
        raise ValueError(f"no such column(s) {missing} in the result; it has {columns}")
    return [columns.index(c) for c in wanted]


def period_over_period(result: dict, columns: list[str] | None, pct: bool) -> dict:
    """Row i vs row i-1: the rows must already be in the order the comparison should follow
    (query_metric orders a time group_by chronologically by default)."""
    cols, rows = result["columns"], result["rows"]
    idx = _resolve_columns(cols, rows, columns)
    suffix = "_change_pct" if pct else "_change"
    new_cols = list(cols) + [f"{cols[i]}{suffix}" for i in idx]
    new_rows = []
    for j, row in enumerate(rows):
        extra = []
        for i in idx:
            prev, cur = (rows[j - 1][i] if j > 0 else None), row[i]
            if prev is None or cur is None:
                extra.append(None)
            elif pct:
                extra.append((cur - prev) / prev if prev else None)
            else:
                extra.append(cur - prev)
        new_rows.append(list(row) + extra)
    return {"columns": new_cols, "rows": new_rows, "row_count": len(new_rows)}


def share_of_total(result: dict, columns: list[str] | None) -> dict:
    cols, rows = result["columns"], result["rows"]
    idx = _resolve_columns(cols, rows, columns)
    totals = {i: sum(r[i] for r in rows if r[i] is not None) for i in idx}
    new_cols = list(cols) + [f"{cols[i]}_share_pct" for i in idx]
    new_rows = [list(row) + [(row[i] / totals[i]) if row[i] is not None and totals[i] else None
                             for i in idx]
                for row in rows]
    return {"columns": new_cols, "rows": new_rows, "row_count": len(new_rows)}


def apply(operation: str, result: dict, columns: list[str] | None) -> dict:
    if operation == "period_over_period_pct":
        return period_over_period(result, columns, pct=True)
    if operation == "period_over_period_change":
        return period_over_period(result, columns, pct=False)
    if operation == "share_of_total":
        return share_of_total(result, columns)
    raise ValueError(f"unknown operation {operation!r}; use one of {OPERATIONS}")
