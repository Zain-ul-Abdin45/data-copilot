"""
Personal data never reaches the model or the interface. dbt's schema.yml marks a column
`meta: {pii: true}` — an editable business decision, the same pattern as the order-status
rules — and every tool result is masked against that list before agent.py ever sees it,
whichever path produced the rows: query_metric's dimensions or run_sql's SELECT list.

Matches by bare column name (no entity__ prefix, no table qualifier), so `SELECT email AS
contact` defeats it. That is a real gap, the same class as search_catalog being keyword-based:
this is a safety net for the common case, not a guarantee against a determined query.
"""
import dbtproject

REDACTED = "[personal data hidden]"


def pii_column_names() -> set[str]:
    """Bare column names marked pii: true anywhere in the dbt manifest (models and seeds)."""
    manifest = dbtproject.load(dbtproject.MANIFEST)
    names = set()
    for node in manifest["nodes"].values():
        if node["resource_type"] not in ("model", "seed"):
            continue
        for col, meta in node.get("columns", {}).items():
            if (meta.get("meta") or {}).get("pii"):
                names.add(col.lower())
    return names


def _bare(col: str) -> str:
    """A MetricFlow dimension name (customer__first_name) or a table-qualified column
    (c.first_name) reduced to the plain column name."""
    return col.rsplit("__", 1)[-1].rsplit(".", 1)[-1].lower()


def mask_rows(columns: list[str], rows: list[list], enabled: bool = True) -> tuple[list[list], list[str]]:
    """Replace every value in a PII column with a fixed placeholder.
    Returns (possibly-new rows, the column names that were masked)."""
    if not enabled:
        return rows, []
    pii = pii_column_names()
    idx = {i for i, c in enumerate(columns) if _bare(c) in pii}
    if not idx:
        return rows, []
    masked = [[REDACTED if i in idx else v for i, v in enumerate(row)] for row in rows]
    return masked, [c for i, c in enumerate(columns) if i in idx]
