"""A stand-in for the agent, so the interface can be started and tested without a model.
Selected with COPILOT_UI_STUB=1. It returns canned answers shaped exactly like agent.ask()."""

MONTHS = [["2026-01-01", 2820.8, 0.0], ["2026-02-01", 2396.41, 0.1538], ["2026-03-01", 1856.89, 0.0625],
          ["2026-04-01", 3040.45, 0.0], ["2026-05-01", 2261.63, 0.0], ["2026-06-01", 3473.4, 0.1]]


def ask(question: str, history: list[dict] | None = None) -> dict:
    trace = [{"tool": "query_metric", "args": {"metrics": ["net_revenue", "refund_rate"],
                                               "group_by": ["metric_time__month"]},
              "result": {"governed": True, "columns": ["metric_time__month", "net_revenue", "refund_rate"],
                         "rows": MONTHS, "row_count": len(MONTHS), "_sql": "SELECT 1 /* stub */"}}]
    text = f"(stub, turn {len(history or []) + 1}) Net revenue peaked in June at 3,473.40."
    return {"answer": text, "llm_answer": text, "trace": trace, "ungrounded": [], "step_limit": False,
            "think": False, "escalated": False, "elapsed_s": 0.1, "wall_s": 0.1,
            "tables_md": "| month | net revenue |\n| --- | ---: |\n| Jan 2026 | 2,820.80 |",
            "footer_md": "\n\n---\nHow this was calculated\n- Governed metric: net_revenue (stub)",
            "ratio_metrics": ["refund_rate"]}
