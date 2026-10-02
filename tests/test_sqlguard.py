"""Regression tests for the SQL guard. Run: python tests/test_sqlguard.py (or pytest)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlguard import SqlRejected, validate  # noqa: E402

TABLES = {"fct_orders", "dim_customers", "stg_payments", "order_status_rules"}

ALLOWED = [
    "select status, count(*) from fct_orders group by 1",
    "select 'drop table x' as word, 'create' as c from fct_orders",  # keywords inside literals
    "select credit_limit from fct_orders",                            # a column named like a keyword
    "with a as (select * from fct_orders) select * from a",
    "select * from analytics.fct_orders limit 5",
    "select o.status from fct_orders o join dim_customers c on c.customer_id = o.customer_id",
    "select * from fct_orders;",
]

REJECTED = [
    "delete from fct_orders",
    "update fct_orders set status = 'x'",
    "select 1; drop table fct_orders",
    "with x as (delete from fct_orders returning *) select * from x",
    "select * into newtbl from fct_orders",
    "select * from fct_orders for update",
    "copy fct_orders to '/tmp/x'",
    "set role postgres",
    "select * from raw.orders",
    "select * from pg_catalog.pg_tables",
    "select * from information_schema.tables",
    "select * from other_db.analytics.fct_orders",
    "select * from secrets",
    "select pg_read_file('/etc/passwd')",
    "select pg_sleep(100)",
    "select * from generate_series(1, 10)",
    "select * from fct_orders where",
    "",
]


def test_allowed():
    for sql in ALLOWED:
        validate(sql, TABLES)


def test_rejected():
    for sql in REJECTED:
        try:
            validate(sql, TABLES)
        except SqlRejected:
            continue
        raise AssertionError(f"should have been rejected: {sql!r}")


def test_limit_is_capped_and_schema_qualified():
    out, _, _ = validate("select * from fct_orders limit 100000", TABLES)
    assert out.endswith("LIMIT 200") and "analytics.fct_orders" in out
    out, _, _ = validate("select * from fct_orders limit 5", TABLES)
    assert out.endswith("LIMIT 5")
    out, _, _ = validate("select * from fct_orders", TABLES)
    assert out.endswith("LIMIT 200")


def test_lineage_resolves_a_plain_column_to_itself():
    _, _, lineage = validate("select status, credit_limit from fct_orders", TABLES)
    assert lineage["status"] == {"status"}
    assert lineage["credit_limit"] == {"credit_limit"}


def test_lineage_resolves_an_alias_to_its_real_source_column():
    # the exact gap privacy.py's docstring used to flag as open: SELECT x AS y must not hide x.
    _, _, lineage = validate("select status as s from fct_orders", TABLES)
    assert lineage["s"] == {"status"}


def test_lineage_resolves_through_a_join_by_qualified_name():
    _, _, lineage = validate(
        "select c.customer_id as cid from fct_orders o join dim_customers c "
        "on c.customer_id = o.customer_id", TABLES)
    assert lineage["cid"] == {"customer_id"}


def test_lineage_resolves_through_a_cte():
    # the same alias gap, one layer removed: a model routing the rename through a CTE instead
    # of the top-level SELECT must not escape lineage either.
    _, _, lineage = validate(
        "with a as (select status as contact from fct_orders) select contact from a", TABLES)
    assert lineage["contact"] == {"status"}


def test_lineage_is_none_for_a_set_operation():
    _, _, lineage = validate(
        "select status from fct_orders union select status from fct_orders", TABLES)
    assert lineage is None


def test_lineage_omits_an_unresolvable_star_column():
    _, _, lineage = validate("select * from fct_orders", TABLES)
    assert lineage == {}


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
