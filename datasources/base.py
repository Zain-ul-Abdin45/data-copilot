"""The contract every engine implements. `db.py`, `catalog.py` and `sqlguard.py` are written
against this, never against a specific driver, so they do not change when an engine is added."""
import datetime as dt
from abc import ABC, abstractmethod
from decimal import Decimal


def jsonable(v):
    """Make a database value safe to hand to the model as JSON."""
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, dt.datetime):
        return v.date().isoformat() if v.time() == dt.time(0) else v.isoformat()
    if isinstance(v, dt.date):
        return v.isoformat()
    return v


class DataSource(ABC):
    """One engine `run_sql` and the catalog run against. `dialect` is the sqlglot dialect name
    used to parse and re-render model-written SQL; `schema` and `catalog` (None for a
    two-level engine like Postgres or DuckDB) are what sqlguard restricts table references to."""

    dialect: str
    schema: str
    catalog: str | None = None

    @abstractmethod
    def query(self, sql: str, params: tuple = ()) -> tuple[list[str], list[list]]:
        """Run one read-only statement; return (column_names, rows) with plain Python values
        (jsonable() applied), never driver-specific types."""

    def query_dicts(self, sql: str, params: tuple = ()) -> list[dict]:
        cols, rows = self.query(sql, params)
        return [dict(zip(cols, r)) for r in rows]

    @abstractmethod
    def columns(self) -> list[tuple[str, str, str]]:
        """(table_name, column_name, data_type) for every table/view in `schema`, ordinal order."""

    def qualify(self, table: str) -> str:
        """`table`, schema-qualified (and catalog-qualified where the engine has one), for SQL
        this code writes itself (the business-rules seeds). Never used for run_sql, which
        sqlguard qualifies and validates on its own terms."""
        return f"{self.schema}.{table}"

    def dbt_env(self) -> dict[str, str]:
        """Environment variables the matching dbt profile target (named settings.DBT_TARGET)
        reads via env_var(); see dbt-test-project/profiles.yml. Empty when the target has
        nothing to fill in from the process environment (e.g. a fixed local file path)."""
        return {}
