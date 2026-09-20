"""
Result tables drawn by code from the tool rows. The model writes the words;
it never types the numbers of a multi-row result, so they cannot be
mis-copied. Single-row results stay in the narrative, where the grounding
check covers them.
"""
MAX_ROWS = 25
MAX_TABLES = 2
_ID_LIKE = ("_id", "__customer", "__order")  # identifiers: no thousands separator


def _fmt(v, column: str, ratio: set[str]) -> str:
    if v is None:
        return "–"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, float):
        return f"{v:.1%}" if column in ratio else f"{v:,.2f}"
    if isinstance(v, int):
        return str(v) if column == "id" or column.endswith(_ID_LIKE) else f"{v:,}"
    return str(v).replace("|", "\\|")


def _is_number_column(rows: list[list], i: int) -> bool:
    vals = [r[i] for r in rows if r[i] is not None]
    return bool(vals) and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in vals)


def _table(columns: list[str], rows: list[list], ratio: set[str]) -> str:
    align = ["---:" if _is_number_column(rows, i) else "---" for i in range(len(columns))]
    lines = ["| " + " | ".join(columns) + " |", "| " + " | ".join(align) + " |"]
    for row in rows[:MAX_ROWS]:
        lines.append("| " + " | ".join(_fmt(v, c, ratio) for v, c in zip(row, columns)) + " |")
    if len(rows) > MAX_ROWS:
        lines.append(f"\n…and {len(rows) - MAX_ROWS} more rows.")
    return "\n".join(lines)


def render(trace: list[dict], ratio_metrics: set[str] | None = None) -> str:
    """Tables for the multi-row results of the last few successful data calls."""
    ratio = ratio_metrics or set()
    seen, blocks = set(), []
    for step in reversed(trace):
        r = step["result"]
        if step["tool"] not in ("query_metric", "run_sql") or "error" in r:
            continue
        if len(r["rows"]) < 2:
            continue
        key = (tuple(r["columns"]), str(r["rows"]))
        if key in seen:
            continue
        seen.add(key)
        label = ("Returned by the governed metric query" if r.get("governed")
                 else "Returned by ad-hoc SQL (not a governed metric)")
        blocks.append(f"{label}:\n\n" + _table(r["columns"], r["rows"], ratio))
        if len(blocks) == MAX_TABLES:
            break
    return "\n\n".join(reversed(blocks))
