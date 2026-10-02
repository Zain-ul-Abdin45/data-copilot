"""
Validates model-written SQL before it reaches the database. This is the
second line of defence: the database role is the first (it cannot write and
cannot see anything outside the analytics schema), so a bug here must not be
able to cause damage, only to let a bad query through to a read-only session.

Parses with sqlglot instead of matching keywords, so `select 'drop' as word`
is fine and `with x as (delete from t returning *) select * from x` is not.
"""
import sqlglot
from sqlglot import exp
from sqlglot.lineage import lineage as _column_lineage

import datasources
import settings


class SqlRejected(ValueError):
    pass


_WRITE_NODES = tuple(
    getattr(exp, n) for n in
    ("Insert", "Update", "Delete", "Merge", "Create", "Drop", "Alter", "Command", "Into", "Copy",
     "Set", "TruncateTable", "Grant", "Transaction", "Use", "Lock")
    if hasattr(exp, n)
)
# Functions that reach outside the query: files, other databases, settings, sleeping.
_BLOCKED_PREFIXES = ("pg_", "lo_", "dblink", "set_config", "current_setting", "query_to_xml",
                     "xpath", "txid_")


def _leaf_names(node) -> set[str]:
    """Bare column names at the leaves of a sqlglot lineage graph (sqlglot.lineage.lineage()),
    e.g. "customers.email" -> "email". A leaf with no real column behind it (a literal, an
    aggregate like count(*)) names itself, which is never a PII name, so it masks to nothing —
    the same as before lineage existed."""
    names: set[str] = set()

    def walk(n) -> None:
        if not n.downstream:
            leaf = n.expression.name if isinstance(n.expression, exp.Column) else n.name.rsplit(".", 1)[-1]
            names.add(leaf.lower())
        for d in n.downstream:
            walk(d)

    walk(node)
    return names


def _lineage(tree: exp.Expression, dialect: str) -> dict[str, set[str]] | None:
    """output column name -> the real source column name(s) feeding it, found by walking
    sqlglot's own lineage graph down to its leaves — resolves through an alias, an expression,
    and even a CTE (`WITH a AS (SELECT email AS contact FROM customers) SELECT contact FROM a`
    still finds "email"). No schema, no database call: lets privacy.py mask by what a column
    actually IS, not what the query calls it, closing the `SELECT email AS contact` gap the
    README used to flag as open. None for a UNION/INTERSECT (lineage() does not span them).
    A column lineage() cannot resolve (e.g. the single projection of a bare `SELECT *`, where
    the real column names come back from the database unchanged anyway) is left out of the
    result; callers fall back to name-based masking for it, never less safe than before this
    existed."""
    if not isinstance(tree, exp.Select):
        return None
    out: dict[str, set[str]] = {}
    for proj in tree.selects:
        alias = (proj.alias_or_name or "").lower()
        if not alias or alias == "*":
            continue
        try:
            node = _column_lineage(alias, tree, dialect=dialect)
        except Exception:
            continue
        out[alias] = _leaf_names(node) or {alias}
    return out


def validate(sql: str, allowed_tables: set[str]) -> tuple[str, list[str], dict[str, set[str]] | None]:
    """Return (safe_sql, tables_used, column_lineage) or raise SqlRejected with a message the
    model can act on. Parses and re-renders in whichever engine is active
    (datasources.get()), so the same guard covers Postgres, DuckDB and Trino."""
    ds = datasources.get()
    try:
        statements = [s for s in sqlglot.parse(sql, read=ds.dialect) if s is not None]
    except sqlglot.errors.SqlglotError as e:
        raise SqlRejected(f"Could not parse the SQL: {str(e)[:200]}")
    if len(statements) != 1:
        raise SqlRejected("Send exactly one SELECT statement.")

    tree = statements[0]
    if not isinstance(tree, (exp.Select, exp.SetOperation)):
        raise SqlRejected("Only SELECT queries are allowed.")
    lineage_source = tree.copy()  # a pristine copy: the loop below mutates `tree` in place
    if tree.find(*_WRITE_NODES) is not None:
        raise SqlRejected("Only read-only SELECT queries are allowed.")
    if tree.args.get("locks"):
        raise SqlRejected("Row locking (FOR UPDATE) is not allowed.")

    for fn in tree.find_all(exp.Anonymous):
        if fn.name.lower().startswith(_BLOCKED_PREFIXES):
            raise SqlRejected(f"Function {fn.name}() is not allowed.")

    cte_names = {cte.alias for cte in tree.find_all(exp.CTE)}
    used = []
    for table in tree.find_all(exp.Table):
        if not isinstance(table.this, exp.Identifier):
            raise SqlRejected("Table-valued functions are not allowed in FROM.")
        name = table.name
        if not table.db and name in cte_names:
            continue
        # ds.catalog is None on a two-level engine (Postgres, DuckDB): any catalog is then wrong.
        # On a three-level engine (Trino) a matching catalog is fine, so only a *mismatched* one
        # (or any catalog at all when there is none to match) is rejected.
        if (table.catalog and table.catalog != ds.catalog) or (not ds.catalog and table.catalog) \
                or (table.db and table.db != ds.schema):
            raise SqlRejected(f"Only the {ds.schema} schema can be queried.")
        if name not in allowed_tables:
            raise SqlRejected(
                f"Unknown table {name!r}. Available tables: {', '.join(sorted(allowed_tables))}.")
        table.set("db", exp.to_identifier(ds.schema))
        if ds.catalog:
            table.set("catalog", exp.to_identifier(ds.catalog))
        used.append(name)

    limit = tree.args.get("limit")
    n = limit.expression.this if limit is not None and isinstance(limit.expression, exp.Literal) else None
    if n is None or not str(n).isdigit() or int(n) > settings.ROW_LIMIT:
        tree = tree.limit(settings.ROW_LIMIT)

    lineage = _lineage(lineage_source, ds.dialect)
    return tree.sql(dialect=ds.dialect), sorted(set(used)), lineage
