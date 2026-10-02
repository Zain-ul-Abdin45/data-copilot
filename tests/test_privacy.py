"""Personal data must never survive into a tool result, whichever path produced the rows.
No LLM, no database: dbtproject.load is mocked with a manifest shaped like a real one."""
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dbtproject  # noqa: E402
import privacy  # noqa: E402

MANIFEST = {"nodes": {
    "model.x.stg_customers": {"resource_type": "model", "columns": {
        "customer_id": {}, "first_name": {"meta": {"pii": True}},
        "email": {"meta": {"pii": True}}}},
    "model.x.fct_orders": {"resource_type": "model", "columns": {
        "order_id": {}, "status": {}}},
    "test.x.some_test": {"resource_type": "test", "columns": {"email": {"meta": {"pii": True}}}},
}}


def _mocked():
    return mock.patch.object(dbtproject, "load", return_value=MANIFEST)


def test_pii_column_names_reads_meta_from_models_only():
    with _mocked():
        assert privacy.pii_column_names() == {"first_name", "email"}  # not "status", not from a test node


def test_masks_a_bare_run_sql_column():
    with _mocked():
        rows, masked = privacy.mask_rows(["customer_id", "email"], [[1, "a@x.com"], [2, "b@x.com"]])
        assert rows == [[1, privacy.REDACTED], [2, privacy.REDACTED]]
        assert masked == ["email"]


def test_masks_a_metricflow_dimension_name():
    with _mocked():
        rows, masked = privacy.mask_rows(["customer__first_name", "metric_time__month"],
                                         [["Ada", "2026-01-01"]])
        assert rows == [[privacy.REDACTED, "2026-01-01"]]
        assert masked == ["customer__first_name"]


def test_masks_a_table_qualified_column():
    with _mocked():
        rows, _ = privacy.mask_rows(["c.email"], [["a@x.com"]])
        assert rows == [[privacy.REDACTED]]


def test_no_pii_columns_present_leaves_rows_untouched():
    with _mocked():
        rows, masked = privacy.mask_rows(["order_id", "status"], [[1, "placed"]])
        assert rows == [[1, "placed"]] and masked == []


def test_disabled_bypasses_masking_entirely():
    with _mocked():
        rows, masked = privacy.mask_rows(["email"], [["a@x.com"]], enabled=False)
        assert rows == [["a@x.com"]] and masked == []


def test_an_aliased_pii_column_is_not_caught_by_bare_name_matching_alone():
    # SELECT email AS contact defeats bare-name matching on its own; this is why sqlguard.validate
    # resolves column_lineage for run_sql (see test_sqlguard.py) and passes it in here.
    with _mocked():
        rows, masked = privacy.mask_rows(["contact"], [["a@x.com"]])
        assert rows == [["a@x.com"]] and masked == []


def test_an_aliased_pii_column_is_caught_when_lineage_is_given():
    with _mocked():
        rows, masked = privacy.mask_rows(["contact"], [["a@x.com"]],
                                         column_lineage={"contact": {"email"}})
        assert rows == [[privacy.REDACTED]] and masked == ["contact"]


def test_lineage_is_consulted_per_column_not_all_or_nothing():
    with _mocked():
        rows, masked = privacy.mask_rows(
            ["contact", "order_id"], [["a@x.com", 1]],
            column_lineage={"contact": {"email"}, "order_id": {"order_id"}})
        assert rows == [[privacy.REDACTED, 1]] and masked == ["contact"]
