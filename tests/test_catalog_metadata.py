"""The UI's schema browser (table_columns, column_info): a real database is mocked out, since
this is what a person clicks "(i)" on, not the model — the PII masking rule is the one thing
that must hold regardless of mocking."""
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import catalog  # noqa: E402


class _FakeDataSource:
    schema = "analytics"

    def columns(self):
        return [("dim_customers", "customer_id", "integer"), ("dim_customers", "email", "text"),
                ("dim_customers", "lifetime_orders", "bigint"), ("fct_orders", "order_id", "integer")]

    def qualify(self, table):
        return f"{self.schema}.{table}"


def test_table_columns_groups_by_table_in_ordinal_order():
    with mock.patch.object(catalog, "datasources", mock.Mock(get=lambda: _FakeDataSource())):
        cols = catalog.table_columns()
    assert cols["dim_customers"] == [("customer_id", "integer"), ("email", "text"), ("lifetime_orders", "bigint")]
    assert cols["fct_orders"] == [("order_id", "integer")]


def test_column_info_rejects_a_table_or_column_not_in_the_real_catalog():
    with mock.patch.object(catalog, "datasources", mock.Mock(get=lambda: _FakeDataSource())):
        assert "error" in catalog.column_info("dim_customers", "not_a_real_column")
        assert "error" in catalog.column_info("not_a_real_table", "customer_id")


def test_column_info_returns_fill_rate_and_sample_for_an_ordinary_column():
    # query_dicts is called once for the count/fill query (one row back) and once for the sample
    # query (several rows back), in that order
    calls = iter([[{"total": 300, "filled": 300}], [{"v": 5}, {"v": 8}, {"v": 3}]])
    with mock.patch.object(catalog, "datasources", mock.Mock(get=lambda: _FakeDataSource())), \
            mock.patch.object(catalog.db, "query_dicts", lambda *a, **kw: next(calls)):
        info = catalog.column_info("dim_customers", "lifetime_orders")
    assert info == {"fill_pct": 100.0, "sample": [5, 8, 3]}


def test_column_info_never_returns_sample_values_for_a_pii_column():
    # email is marked pii: true in the real dbt schema.yml; here the pii list itself is mocked so
    # this test does not depend on that file's current contents, only on column_info obeying it
    calls = iter([[{"total": 300, "filled": 291}]])  # only the fill-rate query should ever run
    with mock.patch.object(catalog, "datasources", mock.Mock(get=lambda: _FakeDataSource())), \
            mock.patch.object(catalog.db, "query_dicts", lambda *a, **kw: next(calls)), \
            mock.patch.object(catalog.privacy, "pii_column_names", lambda: {"email"}):
        info = catalog.column_info("dim_customers", "email")
    assert info == {"fill_pct": 97.0, "masked": True}
    assert "sample" not in info


def test_column_info_handles_an_entirely_empty_table_without_dividing_by_zero():
    calls = iter([[{"total": 0, "filled": 0}], []])  # no rows at all, so no sample either
    with mock.patch.object(catalog, "datasources", mock.Mock(get=lambda: _FakeDataSource())), \
            mock.patch.object(catalog.db, "query_dicts", lambda *a, **kw: next(calls)):
        info = catalog.column_info("fct_orders", "order_id")
    assert info == {"fill_pct": 0.0, "sample": []}
