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


def fake_embed(vectors):
    class R:
        def raise_for_status(self):
            pass

        def json(self):
            return {"embeddings": vectors}
    return R()


def test_embed_returns_vectors_for_the_whole_batch_in_one_call():
    with mock.patch.object(llm.httpx, "post", return_value=fake_embed([[0.1, 0.2], [0.3, 0.4]])) as post:
        assert llm.embed(["a", "b"]) == [[0.1, 0.2], [0.3, 0.4]]
        assert post.call_count == 1  # one request for the batch, not one per text


def test_embed_returns_none_when_unreachable_rather_than_raising():
    with mock.patch.object(llm.httpx, "post", side_effect=ConnectionError("refused")):
        assert llm.embed(["a"]) is None


def test_embed_of_nothing_is_a_no_op():
    assert llm.embed([]) == []


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


def test_must_not_mention_catches_a_wrong_refusal():
    # the original reported bug: a correct answer would still fail here if it also hedged
    # with the wrong-refusal phrasing, which is exactly what this exists to catch
    case = {"must_not_mention": ["not available", "does not exist"]}
    assert run_evals.grade(case, "March saw a 26.9% jump.", [], []) == []
    failures = run_evals.grade(case, "That figure is not available from the warehouse.", [], [])
    assert failures == ["answer wrongly mentions: 'not available'"], failures


def fake_multi_turn_ask(question, history=None, focus_tables=None):
    """A minimal stand-in shaped like agent.ask(): each call's trace/ungrounded/elapsed_s are
    distinguishable so run_turns's concatenation-vs-final-answer split can be checked."""
    turn = len(history or []) + 1
    return {"answer": f"turn {turn} answer", "llm_answer": f"turn {turn} answer",
            "trace": [{"tool": f"tool_{turn}", "args": {}, "result": {"rows": [[1]], "row_count": 1}}],
            "ungrounded": [f"bad_{turn}"] if turn == 1 else [], "elapsed_s": 1.5, "wall_s": 1.5,
            "escalated": turn == 2, "think": False, "step_limit": False}


def test_run_turns_grades_only_the_final_answer_but_combines_trace_and_time():
    result = run_evals.run_turns(fake_multi_turn_ask, ["first question", "second question"])
    assert result["answer"] == "turn 2 answer"  # only the last turn is what a real user reads
    assert [t["tool"] for t in result["trace"]] == ["tool_1", "tool_2"]  # both turns' tools visible
    assert result["ungrounded"] == ["bad_1"]  # an earlier turn's bad figure must still be caught
    assert result["elapsed_s"] == 3.0 and result["wall_s"] == 3.0  # summed, not just the last turn
    assert result["escalated"] is True  # escalated on any turn, not only the last


def test_run_turns_builds_history_the_way_the_ui_does():
    seen_history = []

    def recording_ask(question, history=None, focus_tables=None):
        seen_history.append(list(history or []))
        return {"answer": question, "llm_answer": f"echo: {question}", "trace": [], "ungrounded": [],
                "elapsed_s": 0.1, "wall_s": 0.1, "escalated": False, "think": False, "step_limit": False}

    run_evals.run_turns(recording_ask, ["q1", "q2", "q3"])
    assert seen_history == [[], [{"question": "q1", "answer": "echo: q1"}],
                             [{"question": "q1", "answer": "echo: q1"}, {"question": "q2", "answer": "echo: q2"}]]


def test_close_tolerates_a_percentage_rounding_tie_but_not_a_wrong_figure():
    # -0.0625 * 100 = -6.25, an exact rounding tie; fmt_percent (half up) displays it as "-6.3"
    truth = [(True, -0.0625)]
    assert run_evals.grade({}, "The change was -6.3%.", [], truth) == []
    assert run_evals.grade({}, "The change was -6.2%.", [], truth) == []  # the other side of the tie
    assert run_evals.grade({}, "The change was -8.0%.", [], truth) != []  # a genuinely wrong figure
