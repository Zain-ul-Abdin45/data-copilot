"""
Personal data never reaches the model or the interface. dbt's schema.yml marks a column
`meta: {pii: true}` — an editable business decision, the same pattern as the order-status
rules — and every tool result is masked against that list before agent.py ever sees it,
whichever path produced the rows: query_metric's dimensions or run_sql's SELECT list.

Matches by bare column name (no entity__ prefix, no table qualifier) by default. For run_sql,
sqlguard.validate additionally resolves each output column back to its real source column via
sqlglot's qualifier (column_lineage below), so `SELECT email AS contact` is caught too, not just
a column selected under its own name. That lineage covers single SELECTs that sqlglot can
qualify against the real schema; a UNION/INTERSECT or a construct it cannot qualify falls back
to bare-name matching, same as before this existed — never less safe, just not improved for
that case. query_metric's dimensions have no aliasing of this kind (MetricFlow names them
directly), so bare-name matching is already exact there.
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


def mask_rows(columns: list[str], rows: list[list], enabled: bool = True,
              column_lineage: dict[str, set[str]] | None = None) -> tuple[list[list], list[str]]:
    """Replace every value in a PII column with a fixed placeholder.
    Returns (possibly-new rows, the column names that were masked).

    `column_lineage` (from sqlguard.validate, run_sql only) maps an output column's own name to
    the real source column(s) feeding it; when given, a column is masked if ANY of its real
    sources is PII, catching an alias (`SELECT email AS contact`) that bare-name matching alone
    would miss. Columns absent from the lineage (sqlglot could not qualify the query) fall back
    to bare-name matching for that column only."""
    if not enabled:
        return rows, []
    pii = pii_column_names()

    def sources(col: str) -> set[str]:
        if column_lineage is not None and col.lower() in column_lineage:
            return column_lineage[col.lower()]
        return {_bare(col)}

    idx = {i for i, c in enumerate(columns) if sources(c) & pii}
    if not idx:
        return rows, []
    masked = [[REDACTED if i in idx else v for i, v in enumerate(row)] for row in rows]
    return masked, [c for i, c in enumerate(columns) if i in idx]
