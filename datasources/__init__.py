"""
Which engine `run_sql` and the catalog actually run against: Postgres today, a
local DuckDB file or a Trino cluster tomorrow. The governed path (MetricFlow)
points at the same engine independently, via the dbt profile target named by
`settings.DBT_TARGET` — see each class's `dbt_env()`.

Adding a fourth engine means one new file implementing `DataSource` plus one
line in `_load()`; nothing above this layer needs to know which engine is active.
"""
import settings
from datasources.base import DataSource

_instance: DataSource | None = None


def _load(kind: str) -> DataSource:
    if kind == "postgres":
        from datasources.postgres import PostgresDataSource
        return PostgresDataSource()
    if kind == "duckdb":
        from datasources.duckdb_source import DuckDBDataSource
        return DuckDBDataSource()
    if kind == "trino":
        from datasources.trino import TrinoDataSource
        return TrinoDataSource()
    raise RuntimeError(f"Unknown COPILOT_DATASOURCE {kind!r}; choose postgres, duckdb or trino.")


def get() -> DataSource:
    """The active data source, built once per process from settings.DATASOURCE."""
    global _instance
    if _instance is None:
        _instance = _load(settings.DATASOURCE)
    return _instance


def reset() -> None:
    """For tests: forget the cached instance so the next get() re-reads settings."""
    global _instance
    _instance = None
