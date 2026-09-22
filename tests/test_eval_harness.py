"""The eval harness itself: grader strictness, Ollama readiness, comparison readout.
No LLM, no database."""
import io
import json
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "evals"))

import compare  # noqa: E402
import llm  # noqa: E402
import run_evals  # noqa: E402
import settings  # noqa: E402


def step(tool, error=False):
    return {"tool": tool, "args": {}, "result": {"error": "boom"} if error else {"rows": [[1]], "row_count": 1}}


def test_a_failed_call_does_not_count_as_using_the_tool():
    case = {"tools_required": ["query_metric"]}
    failures = run_evals.grade(case, "x", [step("query_metric", error=True), step("run_sql")], [])
    assert any("did not succeed" in f for f in failures), failures
    assert run_evals.grade(case, "x", [step("query_metric")], []) == []


def test_forbidden_counts_even_failed_calls():
    failures = run_evals.grade({"tools_forbidden": ["run_sql"]}, "x", [step("run_sql", error=True)], [])
    assert failures == ["forbidden tool called: run_sql"]


def test_expected_numbers_and_phrases():
    truth = [(True, 0.0833333), (False, 2147.43)]
    assert run_evals.grade({}, "Refund rate 8.3% and $2,147.43 refunded", [], truth) == []
    missing = run_evals.grade({}, "Refund rate 8.3%", [], truth)
    assert missing == ["expected value missing from answer: 2147.43"], missing
    assert run_evals.grade({"must_mention": ["Not A Governed Metric"]}, "this is not a governed metric", [], []) == []


def fake_tags(names):
    class R:
        def json(self):
            return {"models": [{"name": n} for n in names]}
    return R()


def test_ollama_readiness_messages():
    with mock.patch.object(settings, "MODEL", "qwen3:14b"):
        with mock.patch.object(llm.httpx, "get", return_value=fake_tags(["qwen3:14b", "llama3.1:latest"])):
            assert llm.ready() is None
        with mock.patch.object(llm.httpx, "get", return_value=fake_tags(["llama3.1:latest"])):
            assert "not installed" in llm.ready()
        with mock.patch.object(llm.httpx, "get", side_effect=ConnectionError("refused")):
            assert "not reachable" in llm.ready()


def report(name, passed, failures=()):
    run = {"failures": list(failures), "elapsed_s": 12.0, "escalated": False, "answer": "ans", "tools": []}
    return {"agent": name, "model": "m", "think": False, "escalate": True, "repeat": 1,
            "cases": [{"id": "refund_rate", "category": "metric", "question": "q", "rubric": None,
                       "passed_runs": passed, "runs": [run]}]}


def test_compare_shows_both_files_and_the_failure():
    with tempfile.TemporaryDirectory() as d:
        a, b = Path(d) / "a_metricflow.json", Path(d) / "b_wren.json"
        a.write_text(json.dumps(report("agent:ask", 1)))
        b.write_text(json.dumps(report("wren", 0, ["required tool did not succeed: query_metric"])))
        out = io.StringIO()
        with redirect_stdout(out):
            assert compare.main([str(a), str(b)]) == 0
    text = out.getvalue()
    assert "a_metricflow" in text and "b_wren" in text and "refund_rate" in text
    assert "1/1" in text and "0/1" in text and "required tool did not succeed" in text


def test_compare_names_a_missing_file_instead_of_crashing():
    with tempfile.TemporaryDirectory() as d:
        good = Path(d) / "good.json"
        good.write_text(json.dumps(report("agent:ask", 1)))
        out = io.StringIO()
        with redirect_stdout(out):
            assert compare.main([str(good), str(Path(d) / "never_written.json")]) == 0
            assert compare.main([str(Path(d) / "nope.json")]) == 1
    assert "MISSING" in out.getvalue() and "Nothing to compare" in out.getvalue()


def test_compare_survives_an_interrupted_run_with_no_cases():
    empty = {"agent": "a", "model": "m", "think": False, "escalate": True, "repeat": 2, "cases": []}
    partial = report("agent:ask", 0)
    partial["cases"][0]["runs"] = []  # interrupted before its first answer
    with tempfile.TemporaryDirectory() as d:
        a, b = Path(d) / "a.json", Path(d) / "b.json"
        a.write_text(json.dumps(empty))
        b.write_text(json.dumps(partial))
        with redirect_stdout(io.StringIO()):
            assert compare.main([str(a), str(b)]) == 0


def test_must_mention_any_needs_only_one_phrase():
    case = {"must_mention_any": ["not available", "does not exist"]}
    assert run_evals.grade(case, "That data does not exist here.", [], []) == []
    weak = run_evals.grade(case, "No governed metric matches delivery time.", [], [])
    assert len(weak) == 1 and "says none of" in weak[0]


def test_absent_data_answer_must_say_the_data_is_missing_not_just_the_metric():
    import yaml
    case = next(c for c in yaml.safe_load((ROOT / "evals" / "golden.yaml").read_text())["cases"]
                if c["id"] == "absent_shipping_time")
    weak = ('The term "average delivery time" does not match any governed metric. There is no metric '
            "available in the warehouse that tracks the average delivery time in days.")
    strong = ('The data warehouse does not track "average delivery time" or any related metrics. '
              "No tables, columns, or metrics mention delivery or time.")
    assert run_evals.grade(case, weak, [], []) != [], "a metric-only answer must still fail"
    assert run_evals.grade(case, strong, [], []) == []


PAYMENT_ROWS = [("credit_card", [5554.65]), ("paypal", [4730.67])]


def test_a_true_figure_under_the_wrong_label_fails_even_though_it_appears_in_the_answer():
    # last night's Wren answer: the true net figures sat in a column called gross_revenue, and the
    # prose reported a different, wrong net figure per method
    wrong = ("Net revenue by payment method:\n\n- Credit card: $4,487.40\n- PayPal: $4,312.58\n\n"
             "| payment_method | gross_revenue | net_revenue |\n| --- | ---: | ---: |\n"
             "| credit_card | 5,554.65 | 4,487.40 |\n| paypal | 4,730.67 | 4,312.58 |")
    truth = [(False, 5554.65), (False, 4730.67)]
    assert run_evals.grade({}, wrong, [], truth) == []  # the old grader let this through
    failures = run_evals.grade({}, wrong, [], truth, PAYMENT_ROWS)
    assert len(failures) == 2 and "credit_card is shown as 4,487.40" in failures[0], failures


def test_correct_labelled_figures_pass_in_the_usual_writings():
    for line in ("- Credit card: 5,554.65", "**Credit card**: **$5,554.65**", "credit card - 5,554.65",
                 "Credit card (5,554.65)", "credit_card = 5,554.65"):
        assert run_evals.mislabelled(line, PAYMENT_ROWS) == [], line


def test_running_prose_and_tables_and_footers_are_not_judged():
    rows = [("cancelled", [14.0]), ("placed", [18.0])]
    prose = 'We have 10 in "refunded", 14 in "cancelled", 18 in "placed" and 35 in "shipped".'
    assert run_evals.mislabelled(prose, rows) == []          # the number after a label is the next label's
    table = "| status | n |\n| --- | ---: |\n| cancelled | 99 |"
    assert run_evals.mislabelled(table, rows) == []          # the table is drawn from the query result
    footer = "Fine.\n\n---\nHow this was calculated\n- Query: `where status = 'cancelled': 99`"
    assert run_evals.mislabelled(footer, rows) == []
    assert run_evals.mislabelled("Cancelled orders are the fewest.", rows) == []  # label without a figure


def test_close_tolerates_a_percentage_rounding_tie_but_not_a_wrong_figure():
    # -0.0625 * 100 = -6.25, an exact rounding tie; fmt_percent (half up) displays it as "-6.3"
    truth = [(True, -0.0625)]
    assert run_evals.grade({}, "The change was -6.3%.", [], truth) == []
    assert run_evals.grade({}, "The change was -6.2%.", [], truth) == []  # the other side of the tie
    assert run_evals.grade({}, "The change was -8.0%.", [], truth) != []  # a genuinely wrong figure
