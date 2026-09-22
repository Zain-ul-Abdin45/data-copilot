"""A local DuckDB file as the warehouse — the first step towards a data lake (Path B's
eventual target): no server, reads Parquet/CSV directly, and is what `dbt-duckdb` builds
against.

Read-only is NOT enforced at the connection level here, unlike Postgres's session flag: opening
`read_only=True` while dbt-duckdb (via MetricFlow's cached engine, semantic.py's `_engine()`)
holds its own long-lived read-write connection to the same file fails outright — DuckDB refuses
a second connection to one file "with a different configuration than existing connections". The
choice is between that hard failure and giving up the extra layer, so this connects read-write
like the rest of the process does; sqlguard (parse-time, one read-only SELECT, one schema, a row
cap) is the only thing standing between run_sql and a write on this engine. That is a real,
narrower guarantee than Postgres's role-plus-session belt-and-suspenders — worth knowing before
trusting this engine with anything sqlguard has a bug in.

Known gap: also no server-side per-statement timeout to mirror Postgres's `statement_timeout`;
a query that is merely slow is not cut off (it is still capped to `settings.ROW_LIMIT` rows).
"""
import duckdb

import settings
from datasources.base import DataSource, jsonable


class DuckDBDataSource(DataSource):
    dialect = "duckdb"
    schema = settings.DUCKDB_SCHEMA

    def _connect(self) -> duckdb.DuckDBPyConnection:
        return duckdb.connect(database=settings.DUCKDB_PATH)

    def query(self, sql: str, params: tuple = ()) -> tuple[list[str], list[list]]:
        con = self._connect()
        try:
            cur = con.execute(sql, params)
            cols = [d[0] for d in cur.description]
            return cols, [[jsonable(v) for v in row] for row in cur.fetchall()]
        finally:
            con.close()

    def columns(self) -> list[tuple[str, str, str]]:
        _, rows = self.query(
            "select table_name, column_name, data_type from information_schema.columns "
            "where table_schema = ? order by table_name, ordinal_position", (self.schema,))
        return [tuple(r) for r in rows]

    def dbt_env(self) -> dict[str, str]:
        return {"COPILOT_DUCKDB_PATH": settings.DUCKDB_PATH}
