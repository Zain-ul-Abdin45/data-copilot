"""The guard against meaningless groupings, derived from the real semantic manifest.
No LLM, no database (it only reads target/semantic_manifest.json)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import semantic  # noqa: E402


def test_rates_are_defined_over_the_dimension_their_numerator_counts():
    assert semantic.defining_dimensions("refund_rate") == {"order__status"}
    assert semantic.defining_dimensions("cancellation_rate") == {"order__status"}


def test_other_metrics_stay_groupable():
    for name in ("net_revenue", "gross_revenue", "order_count", "average_order_value",
                 "refunded_order_count", "net_order_count", "no_such_metric"):
        assert semantic.defining_dimensions(name) == set(), name


def test_grouping_a_rate_by_its_own_dimension_is_refused_with_advice():
    r = semantic.query_metric(["refund_rate"], group_by=["order__status"])
    assert "error" in r and "0% or 100%" in r["error"] and "order__status" in r["error"]
    assert "without order__status" in r["hint"]


def test_filtering_a_rate_by_its_own_dimension_is_refused_too():
    r = semantic.query_metric(["cancellation_rate"],
                              filters=[{"dimension": "order__status", "operator": "=", "value": "delivered"}])
    assert "error" in r and "0% or 100%" in r["error"]


def test_a_refused_request_never_reaches_the_engine():
    # if it did, this would need the database and MetricFlow; the guard answers first
    from unittest import mock
    with mock.patch.object(semantic, "_engine", side_effect=AssertionError("engine must not be called")):
        assert "error" in semantic.query_metric(["refund_rate", "net_revenue"], group_by=["order__status"])


class _FakeEngine:
    def __init__(self):
        self.request = None

    def query(self, request):
        import types
        self.request = request
        table = types.SimpleNamespace(rows=[("2026-01-01", 1.0)], column_names=("metric_time__month", "net_revenue"))
        return types.SimpleNamespace(result_df=table, sql="SELECT 1")


def _asked(**kwargs):
    from unittest import mock
    fake = _FakeEngine()
    with mock.patch.object(semantic, "_engine", return_value=fake):
        assert "error" not in semantic.query_metric(["net_revenue"], **kwargs)
    return fake.request


def test_a_time_grouping_is_chronological_unless_another_order_was_asked_for():
    assert list(_asked(group_by=["metric_time__month"]).order_by_names or []) == ["metric_time__month"]
    assert list(_asked(group_by=["metric_time__month"], order_by=["-net_revenue"]).order_by_names or []) == ["-net_revenue"]
    assert not _asked(group_by=["order__status"]).order_by_names
    assert not _asked().order_by_names


def test_metrics_named_after_the_asked_word_outrank_ones_that_only_mention_it():
    # "how much was refunded" once returned five metrics that only mention "refunded" in passing and
    # cut refunded_revenue, the one that answers it, off the list of five
    from unittest import mock
    with mock.patch.object(semantic, "_rules", return_value={"glossary": [], "order_status_rules": []}):
        result = semantic.describe_metrics("how much was refunded")
    names = [m["name"] for m in result["metrics"]]
    assert set(names[:2]) == {"refunded_revenue", "refunded_order_count"}, names
