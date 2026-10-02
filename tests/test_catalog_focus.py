"""The soft table/schema focus for search_catalog: a tie-breaker, never a restriction.
No LLM, no database: _ranked is a pure function over a fake entries list."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from catalog import _in_focus, _metric_tables, _ranked  # noqa: E402


def entry(name, tokens):
    return {"name": name, "_tokens": set(tokens)}


ENTRIES = [entry("fct_orders", {"order", "status", "revenue"}),
           entry("dim_customers", {"customer", "email", "name"}),
           entry("stg_payments", {"payment", "method", "amount"})]


def test_with_no_focus_ranking_is_unchanged():
    out = [e["name"] for _, e in _ranked(ENTRIES, {"order"}, None)]
    assert out == ["fct_orders"]


def test_focus_breaks_a_tie_between_equally_good_matches():
    tied = [entry("fct_orders", {"revenue"}), entry("stg_payments", {"revenue"})]
    assert [e["name"] for _, e in _ranked(tied, {"revenue"}, None)] == ["fct_orders", "stg_payments"]
    assert [e["name"] for _, e in _ranked(tied, {"revenue"}, {"stg_payments"})] == ["stg_payments", "fct_orders"]


def test_focus_never_lets_a_worse_match_outrank_a_better_one():
    # dim_customers matches on 2 terms, fct_orders on 1: focus must not override that
    terms = {"customer", "email", "order"}
    out = [e["name"] for _, e in _ranked(ENTRIES, terms, {"fct_orders"})]
    assert out[0] == "dim_customers", out


def test_focus_on_something_irrelevant_to_the_query_changes_nothing():
    out = [e["name"] for _, e in _ranked(ENTRIES, {"order"}, {"stg_payments"})]
    assert out == ["fct_orders"]  # stg_payments still never matched "order" at all


def test_an_empty_focus_behaves_like_no_focus():
    a = [e["name"] for _, e in _ranked(ENTRIES, {"order"}, set())]
    b = [e["name"] for _, e in _ranked(ENTRIES, {"order"}, None)]
    assert a == b


def test_no_query_vec_behaves_like_before_embeddings_existed():
    out = [e["name"] for _, e in _ranked(ENTRIES, {"order"}, None, query_vec=None)]
    assert out == ["fct_orders"]


def test_query_vec_cannot_pull_in_a_zero_keyword_overlap_entry():
    # the whole point of gating on `if score` first: however similar the embedding, an entry
    # with no keyword overlap at all must never appear in results (NOT_FOUND_NOTE depends on it)
    close = entry("dim_customers", {"customer", "email", "name"})
    close["_embedding"] = [1.0, 0.0]
    out = _ranked([close], {"order"}, None, query_vec=[1.0, 0.0])  # a perfect cosine match
    assert out == []


def test_query_vec_only_reorders_entries_keywords_already_matched():
    a = entry("refund_rate", {"refund", "rate"})
    b = entry("refunded_revenue", {"refund", "revenue"})
    a["_embedding"], b["_embedding"] = [1.0, 0.0], [0.0, 1.0]
    terms = {"refund"}  # both tie on keyword score (1 each)
    # a query vector closer to a's embedding breaks the tie towards a, not b
    out = [e["name"] for _, e in _ranked([a, b], terms, None, query_vec=[0.9, 0.1])]
    assert out == ["refund_rate", "refunded_revenue"]
    out = [e["name"] for _, e in _ranked([a, b], terms, None, query_vec=[0.1, 0.9])]
    assert out == ["refunded_revenue", "refund_rate"]


def test_an_entry_with_no_embedding_is_unaffected_by_query_vec():
    out = [e["name"] for _, e in _ranked(ENTRIES, {"order"}, None, query_vec=[1.0, 0.0])]
    assert out == ["fct_orders"]  # ENTRIES carry no "_embedding"; nothing to re-rank with


def _measure(name):
    return {"name": name}


def _simple(name, measure):
    return {"name": name, "type": "simple", "type_params": {"measure": {"name": measure}}}


def _derived(name, refs):
    return {"name": name, "type": "derived", "type_params": {"metrics": [{"name": r} for r in refs]}}


def _ratio(name, num, den):
    return {"name": name, "type": "ratio",
            "type_params": {"numerator": {"name": num}, "denominator": {"name": den}}}


MANIFEST = {
    "semantic_models": [
        {"node_relation": {"alias": "fct_orders"},
         "measures": [_measure("orders"), _measure("amount_paid"), _measure("amount_refunded")]},
        {"node_relation": {"alias": "dim_customers"}, "measures": [_measure("lifetime_orders")]},
    ],
    "metrics": [
        _simple("order_count", "orders"),
        _simple("gross_revenue", "amount_paid"),
        _simple("refunded_revenue", "amount_refunded"),
        _derived("net_revenue", ["gross_revenue", "refunded_revenue"]),
        _ratio("refund_rate", "refunded_revenue", "order_count"),
        _simple("repeat_customers", "lifetime_orders"),
    ],
}


def test_a_simple_metric_resolves_to_its_measures_table():
    tables = _metric_tables(MANIFEST)
    assert tables["order_count"] == {"fct_orders"}
    assert tables["repeat_customers"] == {"dim_customers"}


def test_a_derived_metric_resolves_through_its_referenced_metrics():
    assert _metric_tables(MANIFEST)["net_revenue"] == {"fct_orders"}


def test_a_ratio_metric_resolves_through_numerator_and_denominator():
    assert _metric_tables(MANIFEST)["refund_rate"] == {"fct_orders"}


def test_an_unknown_metric_name_resolves_to_nothing_not_a_crash():
    assert _metric_tables(MANIFEST).get("no_such_metric", set()) == set()


def test_focusing_a_table_reaches_the_metrics_built_on_it():
    tables = _metric_tables(MANIFEST)
    net_revenue = {"name": "net_revenue", "tables": sorted(tables["net_revenue"])}
    repeat_customers = {"name": "repeat_customers", "tables": sorted(tables["repeat_customers"])}
    assert _in_focus(net_revenue, {"fct_orders"}) is True
    assert _in_focus(net_revenue, {"dim_customers"}) is False
    assert _in_focus(repeat_customers, {"dim_customers"}) is True


def test_metric_level_focus_end_to_end_through_ranked():
    tables = _metric_tables(MANIFEST)
    entries = [
        {"name": "net_revenue", "_tokens": {"revenue"}, "tables": sorted(tables["net_revenue"])},
        {"name": "gross_revenue", "_tokens": {"revenue"}, "tables": sorted(tables["gross_revenue"])},
    ]
    # tied on the query term; focusing fct_orders (both are built on it) changes nothing,
    # focusing dim_customers (neither is) changes nothing either — only the metric's own name
    # would, and here both are equally "in focus" or equally not
    out = [e["name"] for _, e in _ranked(entries, {"revenue"}, {"fct_orders"})]
    assert out == sorted(out)  # falls back to the existing alphabetical tie-break, unaffected
