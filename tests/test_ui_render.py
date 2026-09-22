"""What the interface shows: badges, charts, notes, details. No browser, no model, no database."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ui"))

import render  # noqa: E402


def step(tool, columns=None, rows=None, error=None, sql=None, args=None):
    result = {"error": error} if error else {"columns": columns, "rows": rows, "row_count": len(rows),
                                             "governed": tool == "query_metric"}
    if sql:
        result["_sql"] = sql
    return {"tool": tool, "args": args or {}, "result": result}


MONTHS = step("query_metric", ["metric_time__month", "net_revenue"],
              [["2026-01-01", 2820.8], ["2026-02-01", 2396.41], ["2026-03-01", 1856.89]])
STATUS = step("query_metric", ["order__status", "order_count"],
              [["cancelled", 14], ["delivered", 43], ["placed", 18], ["refunded", 10], ["shipped", 35]])
CUSTOMERS = step("query_metric", ["order__customer", "net_revenue"], [[23, 2331.43], [25, 1763.6], [21, 1462.79]])


def result(trace, **extra):
    base = {"trace": trace, "llm_answer": "Text.", "ungrounded": [], "escalated": False, "elapsed_s": 12.0,
            "tables_md": "TABLE", "footer_md": "\n\n---\nHow this was calculated\n- Governed metric: net_revenue (x)",
            "ratio_metrics": ["refund_rate"]}
    return base | extra


def test_badge_names_the_weakest_source():
    assert render.classify([MONTHS])[0] == "governed"
    assert render.classify([MONTHS, step("run_sql", ["a", "b"], [[1, 2]])])[0] == "adhoc"
    assert "not a governed metric" in render.classify([step("run_sql", ["a"], [[1]])])[1]
    assert render.classify([{"tool": "search_catalog", "args": {}, "result": {"matches": []}}])[0] == "catalog"
    assert render.classify([])[0] == "none"
    assert render.classify([step("query_metric", error="refused")])[0] == "none"


def test_a_trend_is_a_line_with_readable_month_labels():
    (spec,) = render.chart_specs([MONTHS])
    assert spec["kind"] == "line" and spec["labels"] == ["Jan 2026", "Feb 2026", "Mar 2026"]
    assert spec["title"] == "Net revenue by month" and not spec["pct"] and not spec["horizontal"]


def test_categories_are_bars_and_customer_ids_get_a_label():
    (spec,) = render.chart_specs([STATUS])
    assert spec["kind"] == "bar" and spec["labels"][0] == "cancelled"
    (spec,) = render.chart_specs([CUSTOMERS])
    assert spec["labels"] == ["Customer 23", "Customer 25", "Customer 21"]


def test_a_rate_and_an_amount_never_share_an_axis():
    both = step("query_metric", ["metric_time__month", "net_revenue", "refund_rate"],
                [["2026-01-01", 100.0, 0.1], ["2026-02-01", 200.0, 0.2], ["2026-03-01", 150.0, 0.0]])
    amount, rate = render.chart_specs([both], {"refund_rate"})
    assert [s["name"] for s in amount["series"]] == ["Net revenue"] and not amount["pct"]
    assert [s["name"] for s in rate["series"]] == ["Refund rate"] and rate["pct"]


def test_tables_are_the_better_form_when_a_chart_would_mislead():
    single = step("query_metric", ["net_revenue"], [[1.0]])
    many = step("query_metric", ["order__status", "n"], [[f"s{i}", i] for i in range(13)])
    four = step("query_metric", ["metric_time__month", "a", "b", "c", "d"],
                [["2026-01-01", 1, 2, 3, 4], ["2026-02-01", 2, 3, 4, 5], ["2026-03-01", 3, 4, 5, 6]])
    text_only = step("run_sql", ["name", "note"], [["a", "x"], ["b", "y"]])
    for case in (single, many, four, text_only):
        assert render.chart_specs([case]) == []
    assert render.chart_specs([MONTHS, step("query_metric", error="boom")]) == render.chart_specs([MONTHS])


def test_figure_follows_the_mark_rules_in_both_themes():
    (line,) = render.chart_specs([MONTHS])
    (bars,) = render.chart_specs([STATUS])
    for theme in ("light", "dark"):
        t = render.THEMES[theme]
        fig = render.make_figure(line, theme).to_dict()
        trace = fig["data"][0]
        assert trace["line"]["width"] == 2 and trace["marker"]["size"] >= 8
        assert trace["line"]["color"] == t["series"][0] and trace["marker"]["line"]["color"] == t["surface"]
        assert [x for x in trace["text"] if x] == ["1,856.89"], "only the end of the line is labelled"
        assert fig["layout"]["showlegend"] is False and fig["layout"]["paper_bgcolor"] == t["surface"]
        b = render.make_figure(bars, theme).to_dict()["data"][0]
        assert b["marker"]["cornerradius"] == 4 and [x for x in b["text"] if x] == ["43.00"]


def test_two_series_get_a_legend_and_distinct_palette_slots():
    spec = {"kind": "line", "horizontal": False, "title": "t", "labels": ["a", "b", "c"], "pct": False,
            "series": [{"name": "X", "values": [1, 2, 3], "pct": False}, {"name": "Y", "values": [3, 2, 1], "pct": False}]}
    fig = render.make_figure(spec, "light").to_dict()
    assert fig["layout"]["showlegend"] is True
    assert [d["line"]["color"] for d in fig["data"]] == render.THEMES["light"]["series"][:2]


def test_percent_axis_and_labels_for_rates():
    spec = render.chart_specs([step("query_metric", ["metric_time__month", "refund_rate"],
                                    [["2026-01-01", 0.0], ["2026-02-01", 0.1538], ["2026-03-01", 0.0625]])], {"refund_rate"})[0]
    fig = render.make_figure(spec).to_dict()
    assert fig["layout"]["yaxis"]["tickformat"] == ".0%" and [x for x in fig["data"][0]["text"] if x] == ["6.3%"]


def test_view_carries_the_warnings_the_person_must_see():
    view = render.build_view(result([MONTHS], ungrounded=["$1,885.63"], escalated=True,
                                    escalation_reason="used ad-hoc SQL"))
    text = render.compose(view)
    assert text.startswith("**Governed metric**") and "TABLE" in text
    assert "> **Warning.**" in text and "$1,885.63" in text
    assert "Checked again with deeper reasoning: the first pass used ad-hoc SQL." in text


def test_details_show_the_definitions_steps_and_sql_including_refusals():
    trace = [step("query_metric", error="refund_rate is defined over order__status", args={"metrics": ["refund_rate"]}),
             step("query_metric", ["net_revenue"], [[20486.14]], sql="SELECT 1", args={"metrics": ["net_revenue"]})]
    md = render.details_md(result(trace))
    assert "Governed metric: net_revenue" in md and "refused or failed: refund_rate is defined" in md
    assert "```sql" in md and "SELECT 1" in md and "1 row(s)" in md and "12 s" in md
    assert "No data query" in render.details_md(result([], footer_md=""))


def test_history_keeps_the_words_not_the_tables():
    entry = render.history_entry("Net revenue?", result([MONTHS], llm_answer="  It is 20,486.14.  "))
    assert entry == {"question": "Net revenue?", "answer": "It is 20,486.14."}


def test_the_palette_is_the_one_that_passed_the_validator():
    """Validated with the dataviz skill's validate_palette.js on 2026-09-20 (3 slots, all checks
    pass; light aqua is 2.74:1 on the surface, so a table view must always accompany a chart).
    Changing a colour means re-running that validator, so the values are pinned here."""
    assert render.THEMES["light"]["series"] == ["#2a78d6", "#eb6834", "#1baf7a"]
    assert render.THEMES["dark"]["series"] == ["#3987e5", "#d95926", "#199e70"]
    assert (render.THEMES["light"]["surface"], render.THEMES["dark"]["surface"]) == ("#fcfcfb", "#1a1a19")


def test_a_trend_is_sorted_left_to_right_even_if_the_rows_arrive_scrambled():
    scrambled = step("query_metric", ["metric_time__month", "net_revenue"],
                     [["2026-04-01", 4.0], ["2026-02-01", 2.0], ["2026-03-01", 3.0], ["2026-01-01", 1.0]])
    (spec,) = render.chart_specs([scrambled])
    assert spec["labels"] == ["Jan 2026", "Feb 2026", "Mar 2026", "Apr 2026"]
    assert spec["series"][0]["values"] == [1.0, 2.0, 3.0, 4.0], "values must move with their labels"


def test_two_label_columns_become_one_label_and_a_sensible_title():
    names = step("query_metric", ["customer__first_name", "customer__last_name", "net_revenue"],
                 [["First23", "Last23", 2331.43], ["First25", "Last25", 1763.6], ["First21", "Last21", 1462.79]])
    (spec,) = render.chart_specs([names])
    assert spec["labels"] == ["First23 Last23", "First25 Last25", "First21 Last21"]
    assert spec["title"] == "Net revenue by customer"


def test_rounding_is_half_up_so_the_table_agrees_with_how_people_round():
    import tables
    assert tables.fmt_percent(0.0625) == "6.3%" and tables.fmt_percent(0.0833333) == "8.3%"
    assert tables.fmt_number(2.675) == "2.68" and tables.fmt_number(20486.135) == "20,486.14"
    assert tables.fmt_number(1234567.5) == "1,234,567.50"


def test_a_list_that_only_repeats_the_table_is_dropped_but_other_content_is_kept():
    months = step("query_metric", ["metric_time__month", "net_revenue"],
                  [["2026-01-01", 2820.8], ["2026-02-01", 2396.41], ["2026-03-01", 1856.89]])
    said = "Net revenue by month:\n\n- January: 2,820.80\n- February: 2,396.41\n- March: 1,856.89\n\nMarch was the lowest."
    assert render.strip_listing(said, [months]) == "Net revenue by month:\n\nMarch was the lowest."
    invented = said.replace("1,856.89", "9,999.99")
    assert render.strip_listing(invented, [months]) == invented, "a figure not in the rows must be kept"
    two = "Two:\n- January: 2,820.80\n- February: 2,396.41"
    assert render.strip_listing(two, [months]) == two, "short lists are left alone"
    long_note = said.replace("- January: 2,820.80", "- January: 2,820.80, which was the best month in the whole first half of the year")
    assert render.strip_listing(long_note, [months]) == long_note, "annotated lines carry information"
    assert render.strip_listing(said, []) == said, "with no table there is nothing to be redundant with"


def test_percent_lists_match_the_table_at_the_precision_stated():
    rates = step("query_metric", ["metric_time__month", "refund_rate"],
                 [["2026-01-01", 0.0], ["2026-02-01", 0.1538], ["2026-03-01", 0.0625]])
    said = "Rates:\n- January: 0.0%\n- February: 15.4%\n- March: 6.25%"
    assert render.strip_listing(said, [rates]) == "Rates."


def test_a_percent_over_100_does_not_block_stripping_the_whole_list():
    # net_revenue_growth_mom: August's +166.2% (raw 1.6617...) is outside +/-1, which used to
    # keep it out of `cells`, so the "all lines redundant" check failed and nothing was stripped
    growth = step("query_metric", ["metric_time__month", "net_revenue_growth_mom"],
                  [["2026-02-01", -0.150], ["2026-03-01", -0.225], ["2026-08-01", 1.6617333480753738]])
    said = ("Growth:\n\n- February: -15.0%\n- March: -22.5%\n- August: +166.2%\n\n"
            "August had the largest swing.")
    assert render.strip_listing(said, [growth]) == "Growth:\n\nAugust had the largest swing."


def test_a_leadin_ending_in_a_colon_does_not_dangle_once_its_list_is_stripped():
    months = step("query_metric", ["metric_time__month", "net_revenue"],
                  [["2026-01-01", 2820.8], ["2026-02-01", 2396.41], ["2026-03-01", 1856.89]])
    said = "Net revenue by month is as follows:\n\n- January: 2,820.80\n- February: 2,396.41\n- March: 1,856.89"
    assert render.strip_listing(said, [months]) == "Net revenue by month is as follows."
    # a colon followed by content that survives the strip is not "dangling": left alone
    both = said + "\n\nMarch was the lowest."
    assert render.strip_listing(both, [months]) == "Net revenue by month is as follows:\n\nMarch was the lowest."
