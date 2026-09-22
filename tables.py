"""
Result tables drawn by code from the tool rows. The model writes the words;
it never types the numbers of a multi-row result, so they cannot be
mis-copied. Single-row results stay in the narrative, where the grounding
check covers them.
"""
from decimal import ROUND_HALF_UP, Decimal

MAX_ROWS = 25
MAX_TABLES = 2
_ID_LIKE = ("_id", "__customer", "__order")  # identifiers: no thousands separator


def fmt_number(v: float) -> str:
    """Two decimals, rounded half up like a person would (6.625 -> 6.63), not half to even."""
    return f"{Decimal(repr(v)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):,.2f}"


def fmt_percent(v: float) -> str:
    """A fraction as a percentage with one decimal, half up (0.0625 -> 6.3%)."""
    return f"{(Decimal(repr(v)) * 100).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP):.1f}%"


def _fmt(v, column: str, ratio: set[str]) -> str:
    if v is None:
        return "–"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, float):
        return fmt_percent(v) if column in ratio else fmt_number(v)
    if isinstance(v, int):
        return str(v) if column == "id" or column.endswith(_ID_LIKE) else f"{v:,}"
    return str(v).replace("|", "\\|")


def _trim_boundary_nulls(rows: list[list]) -> list[list]:
    """Drop a leading/trailing row where every value but the first (the group_by label or time
    bucket) is None. A period-over-period metric (e.g. month-over-month growth) has no prior
    period for the first row in a range, and MetricFlow's time-spine join can add one extra
    trailing bucket past the last real one — an artifact of the join, not a real result, and
    already shown honestly as "null" by the tool, never a fabricated number."""
    def empty(row):
        return all(v is None for v in row[1:])
    while len(rows) > 2 and empty(rows[0]):
        rows = rows[1:]
    while len(rows) > 2 and empty(rows[-1]):
        rows = rows[:-1]
    return rows


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
        if step["tool"] not in ("query_metric", "run_sql", "derive") or "error" in r:
            continue
        rows = _trim_boundary_nulls(r["rows"]) if len(r["columns"]) > 1 else r["rows"]
        if len(rows) < 2:
            continue
        key = (tuple(r["columns"]), str(rows))
        if key in seen:
            continue
        seen.add(key)
        if step["tool"] == "derive":
            label = f"Computed from the result above ({r['_derived_from']}), not a fresh query"
        else:
            label = ("Returned by the governed metric query" if r.get("governed")
                     else "Returned by ad-hoc SQL (not a governed metric)")
        blocks.append(f"{label}:\n\n" + _table(r["columns"], rows, ratio))
        if len(blocks) == MAX_TABLES:
            break
    return "\n\n".join(reversed(blocks))
