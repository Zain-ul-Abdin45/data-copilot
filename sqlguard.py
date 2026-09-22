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


def validate(sql: str, allowed_tables: set[str]) -> tuple[str, list[str]]:
    """Return (safe_sql, tables_used) or raise SqlRejected with a message the
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

    return tree.sql(dialect=ds.dialect), sorted(set(used))
