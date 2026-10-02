"""The sidebar's table tree: pure grouping logic, no browser, no running Chainlit server."""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ui"))

os.environ.setdefault("COPILOT_UI_STUB", "1")  # app.py branches on this at import time
import app  # noqa: E402


def names(group) -> list[str]:
    return [t["name"] for t in group["tables"]]


def test_tables_are_grouped_by_dbt_naming_convention():
    groups = app.grouped_tables(["fct_orders", "dim_customers", "stg_orders", "stg_payments"])
    assert [(g["label"], names(g)) for g in groups] == [
        ("Facts", ["fct_orders"]), ("Dimensions", ["dim_customers"]), ("Staging", ["stg_orders", "stg_payments"]),
    ]


def test_each_table_carries_its_columns_empty_by_default():
    groups = app.grouped_tables(["fct_orders"], {"fct_orders": ["order_id", "status"]})
    assert groups == [{"label": "Facts", "tables": [{"name": "fct_orders", "columns": ["order_id", "status"]}]}]
    assert app.grouped_tables(["fct_orders"]) == [{"label": "Facts", "tables": [{"name": "fct_orders", "columns": []}]}]


def test_unrecognised_prefixes_fall_back_to_reference_and_sort_within_a_group():
    groups = app.grouped_tables(["glossary", "order_status_rules", "fct_orders"])
    by_label = {g["label"]: names(g) for g in groups}
    assert by_label["Reference"] == ["glossary", "order_status_rules"]  # sorted, not input order
    assert by_label["Facts"] == ["fct_orders"]


def test_empty_groups_are_dropped_not_sent_as_empty():
    groups = app.grouped_tables(["fct_orders"])
    assert [g["label"] for g in groups] == ["Facts"]  # no Dimensions/Staging/Reference entries


def test_no_tables_means_no_groups():
    assert app.grouped_tables([]) == []


def test_tables_action_is_a_fresh_instance_every_time():
    # Action.send() mutates self.forId, so one shared instance reused across messages would have
    # every earlier message's button silently repoint to whichever message sent it last
    a1, a2 = app.tables_action(), app.tables_action()
    assert a1.id != a2.id
    assert a1.name == "show_tables" and a1.label == "Tables"


def test_stub_available_tables_is_independent_of_the_database():
    tables = app.available_tables()
    assert "fct_orders" in tables and "dim_customers" in tables


def test_stub_mode_never_fetches_real_columns():
    # COPILOT_UI_STUB=1 for this whole test file (see the module-level setdefault above): the
    # tables exist (so the sidebar still has something to show), but every column list is empty,
    # since stub mode makes no database call at all
    cbt = app._columns_by_table()
    assert set(cbt) == {"fct_orders", "dim_customers", "stg_orders", "stg_payments", "stg_customers"}
    assert all(cols == [] for cols in cbt.values())


def test_available_tables_and_columns_by_table_agree_on_which_tables_exist():
    # on_chat_start must not query the database twice for the same thing: available_tables() is
    # derived from _columns_by_table(), not a second independent lookup
    assert set(app.available_tables()) == set(app._columns_by_table())
