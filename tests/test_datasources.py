"""Regression tests for the multi-engine split: sqlguard must behave the same on Postgres and
DuckDB (both two-level: schema.table) and correctly on Trino (three-level: catalog.schema.table),
using only the DataSource's declared dialect/schema/catalog — no real connection, no LLM."""
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import datasources  # noqa: E402
from datasources.base import DataSource, jsonable  # noqa: E402
from sqlguard import SqlRejected, validate  # noqa: E402

TABLES = {"fct_orders", "dim_customers"}


class _Fake(DataSource):
    def __init__(self, dialect, schema, catalog=None):
        self.dialect, self.schema, self.catalog = dialect, schema, catalog

    def query(self, sql, params=()):
        raise AssertionError("sqlguard must not run a query")

    def columns(self):
        raise AssertionError("unused here")


def _with(ds):
    return mock.patch.object(datasources, "get", return_value=ds)


def test_jsonable_covers_the_types_a_driver_can_hand_back():
    import datetime as dt
    from decimal import Decimal
    assert jsonable(Decimal("1.50")) == 1.5
    assert jsonable(dt.date(2026, 1, 1)) == "2026-01-01"
    assert jsonable(dt.datetime(2026, 1, 1, 0, 0)) == "2026-01-01"       # midnight: date only
    assert jsonable(dt.datetime(2026, 1, 1, 9, 30)) == "2026-01-01T09:30:00"
    assert jsonable("x") == "x" and jsonable(3) == 3


def test_duckdb_is_two_level_like_postgres():
    with _with(_Fake("duckdb", "analytics")):
        out, used, _ = validate("select * from fct_orders limit 5000", TABLES)
        assert "analytics.fct_orders" in out and out.endswith("LIMIT 200"), out
        assert used == ["fct_orders"]
        try:
            validate("select * from other.fct_orders", TABLES)
            raise AssertionError("a second schema must be rejected on a two-level engine")
        except SqlRejected:
            pass


def test_trino_qualifies_with_catalog_and_accepts_a_matching_one():
    with _with(_Fake("trino", "analytics", catalog="warehouse")):
        out, _, _ = validate("select * from fct_orders", TABLES)
        assert "warehouse.analytics.fct_orders" in out, out
        out2, _, _ = validate("select * from warehouse.analytics.fct_orders", TABLES)  # already qualified
        assert "warehouse.analytics.fct_orders" in out2


def test_trino_rejects_a_different_catalog():
    with _with(_Fake("trino", "analytics", catalog="warehouse")):
        try:
            validate("select * from other_catalog.analytics.fct_orders", TABLES)
            raise AssertionError("a mismatched catalog must be rejected")
        except SqlRejected:
            pass


def test_a_catalog_is_rejected_on_a_two_level_engine():
    # e.g. a model that has seen Trino syntax before, trying it against the Postgres/DuckDB target
    with _with(_Fake("duckdb", "analytics")):
        try:
            validate("select * from some_catalog.analytics.fct_orders", TABLES)
            raise AssertionError("a catalog reference must be rejected when the engine has none")
        except SqlRejected:
            pass


def test_default_qualify_is_schema_dot_table_trino_is_three_level():
    assert _Fake("postgres", "analytics").qualify("glossary") == "analytics.glossary"
    from datasources.trino import TrinoDataSource
    ds = TrinoDataSource.__new__(TrinoDataSource)  # skip __init__, only qualify() is under test
    ds.schema, ds.catalog = "analytics", "warehouse"
    assert ds.qualify("glossary") == "warehouse.analytics.glossary"
