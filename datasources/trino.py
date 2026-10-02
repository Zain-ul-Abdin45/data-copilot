"""A Trino cluster as the warehouse — the other half of Path B's data-lake step (querying
Parquet/Iceberg on S3/HDFS through a federated engine rather than a single DuckDB file).

Verified against a real (local, throwaway) cluster: `trino/docker-compose.yml` in the repo
root runs `trinodb/trino` with a `postgresql` connector catalog pointed at this project's own
Postgres database through the same read-only `copilot_ro` role — see README's **Data sources**
for the exact commands. `evals/oracle.py --engine metricflow` passed all 18 cases through it
with zero failures and zero expected gaps, including top-N with a real ORDER BY (the one thing
the Wren bake-off engine could not do) and a `net_revenue` identical to the direct-Postgres
answer. Needs the `trino` package (`pip install trino`) and, for the governed path, the
`dbt-trino` adapter.
"""
import settings
from datasources.base import DataSource, jsonable


class TrinoDataSource(DataSource):
    dialect = "trino"
    schema = settings.TRINO_SCHEMA
    catalog = settings.TRINO_CATALOG

    def qualify(self, table: str) -> str:
        return f"{self.catalog}.{self.schema}.{table}"

    def _connect(self):
        import trino.dbapi  # imported lazily: most installs never touch this engine

        return trino.dbapi.connect(
            host=settings.TRINO_HOST, port=settings.TRINO_PORT, user=settings.TRINO_USER,
            catalog=settings.TRINO_CATALOG, schema=settings.TRINO_SCHEMA,
        )

    def query(self, sql: str, params: tuple = ()) -> tuple[list[str], list[list]]:
        # The trino client's paramstyle is unverified here; run_sql's SQL is fully rendered by
        # sqlguard before it reaches this method, so params is unused in practice (kept for
        # interface parity with the other engines).
        con = self._connect()
        try:
            cur = con.cursor()
            cur.execute(sql)
            cols = [d[0] for d in cur.description]
            return cols, [[jsonable(v) for v in row] for row in cur.fetchall()]
        finally:
            con.close()

    def columns(self) -> list[tuple[str, str, str]]:
        # Trino's information_schema is per-catalog: {catalog}.information_schema.columns.
        # self.schema is a trusted config value (COPILOT_TRINO_SCHEMA), not user input.
        _, rows = self.query(
            f"select table_name, column_name, data_type from {self.catalog}.information_schema.columns "
            f"where table_schema = '{self.schema}' order by table_name, ordinal_position")
        return [tuple(r) for r in rows]

    def dbt_env(self) -> dict[str, str]:
        return {"COPILOT_TRINO_HOST": settings.TRINO_HOST, "COPILOT_TRINO_PORT": str(settings.TRINO_PORT),
                "COPILOT_TRINO_USER": settings.TRINO_USER, "COPILOT_TRINO_CATALOG": settings.TRINO_CATALOG,
                "COPILOT_TRINO_SCHEMA": settings.TRINO_SCHEMA}
