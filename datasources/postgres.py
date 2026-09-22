"""The original, verified engine. Safety comes from the database, not from the prompt or from
string checks: the role has SELECT on one schema, sessions are read-only, every statement has
a timeout."""
import psycopg

import settings
from datasources.base import DataSource, jsonable


class PostgresDataSource(DataSource):
    dialect = "postgres"
    schema = settings.SCHEMA

    def _connect(self) -> psycopg.Connection:
        conn = psycopg.connect(
            host=settings.DB_HOST,
            dbname=settings.DB_NAME,
            user=settings.DB_USER,
            password=settings.DB_PASSWORD,
            options=(f"-c default_transaction_read_only=on "
                     f"-c statement_timeout={settings.STATEMENT_TIMEOUT_MS} "
                     f"-c search_path={settings.SCHEMA}"),
        )
        conn.read_only = True
        return conn

    def query(self, sql: str, params: tuple = ()) -> tuple[list[str], list[list]]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            cols = [d.name for d in cur.description]
            return cols, [[jsonable(v) for v in row] for row in cur.fetchall()]

    def columns(self) -> list[tuple[str, str, str]]:
        cols, rows = self.query(
            "select table_name, column_name, data_type from information_schema.columns "
            "where table_schema = %s order by table_name, ordinal_position", (self.schema,))
        return [tuple(r) for r in rows]

    def dbt_env(self) -> dict[str, str]:
        # Forced only while dbt/MetricFlow reads the profile (see semantic.py._engine): left in
        # os.environ these would leak into every other connection of this process as copilot_ro.
        return {"PGUSER": settings.DB_USER, "PGPASSWORD": settings.DB_PASSWORD, "PGHOST": settings.DB_HOST}
