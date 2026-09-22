"""
The agent loop: one system prompt, four tools, a bounded number of rounds.

The model never gets to describe how a number was produced. The "How this was
calculated" footer is built from the tool trace by code, so a governed metric
and an ad-hoc SQL result are always labelled correctly, whatever the model says.
"""
import datetime as dt
import json
import time

import audit
import grounding
import semantic
import settings
import tables
from llm import chat
from tools import TOOL_IMPLS, TOOL_SPECS

MAX_STEPS = 8

_TOOLS_WITH_SQL = """Tools
- describe_metrics(term): resolve a business word to a governed metric; returns its definition, \
valid group_by names and the business rules.
- query_metric(...): numbers for governed metrics. Always preferred.
- search_catalog(query): which tables, columns and metrics exist.
- run_sql(sql): last resort, read-only SQL when no governed metric or group_by can answer."""

_TOOLS_NO_SQL = """Tools
- describe_metrics(term): resolve a business word to a governed metric; returns its definition, \
valid group_by names and the business rules.
- query_metric(...): numbers for governed metrics. Always preferred.
- search_catalog(query): which tables, columns and metrics exist.
There is no ad-hoc SQL tool on this deployment. If no governed metric or group_by answers the \
question, say plainly that it cannot be answered from what is available here; never guess at a \
number or fall back to reasoning about numbers you were not given."""

_STEP_2_WITH_SQL = """2. Only if the question needs something no metric or group_by offers (for example a breakdown by \
a column that is not in group_by_options), find the table with search_catalog, then use run_sql. \
Money in SQL must follow the business rules: join order_status_rules on status and use its flags. \
Say plainly that this figure is not a governed metric."""

_STEP_2_NO_SQL = """2. If the question needs something no metric or group_by offers, call search_catalog anyway \
so the footer shows what was checked, then say plainly this cannot be answered from what is \
available here. Do not estimate or reason your way to a number."""

_SYSTEM_PROMPT_TEMPLATE = """You are a data copilot for a company's warehouse. You answer only \
from what your tools return.

{tools}

How to work
1. A question about a number ("revenue", "refund rate", "top customers by revenue"): call \
describe_metrics to resolve the word, then query_metric. Call it with no group_by and no dates unless the \
question names a breakdown ("by month", "per customer", "each status") or a period: the answer is then one \
overall number. Never group by a day or any other dimension on your own. Never write SQL for a governed metric. Check group_by_options: if the breakdown you need is listed there (for example order__status), you must use query_metric, not run_sql.
{step_2}
3. "Do we have / track X": use search_catalog (or describe_metrics for a metric) and answer from \
metadata. Do not query data. If terms_with_no_match names what was asked about, say plainly that \
this data is not available in the warehouse; do not talk about governed metrics or present \
look-alikes as if they answered it. Never stop at "no governed metric matches": say whether the \
data itself exists. This step is about whether the underlying data exists at all, never about a \
calculation over data you already have or can get with query_metric (see step 8): "not available" \
must never be the answer to a question that is really arithmetic on numbers within reach.
4. "How is X calculated": use describe_metrics, then explain the formula and the business rules \
in plain words.
5. Open-ended ("would X help with Y"): you must first call query_metric for the baseline metrics \
involved (for example refund_rate and net_revenue as single overall numbers), even if the data \
cannot answer the question itself. Then reason. Keep "What the data shows" (the numbers you \
retrieved) separate from "My inference", and say plainly when the data cannot answer (for example \
cause and effect).
6. You are read-only. If asked to change or delete data, decline and say why.
7. If a tool returns an error, fix the call using the error and hint and try again (up to three \
times) before you tell the user it cannot be done.
8. A change over time ("month on month", "growth", "change from last period") or a share \
("percentage of total", "what share is X") of a result you already have or one query_metric \
call away: call derive with the operation, never compute this yourself and never answer "not \
available" when the numbers behind it are already in a tool result or a query away. Check \
describe_metrics first only for the metric itself (a named ..._growth_... governed metric, for \
example net_revenue_growth_mom, is exact where one exists and is preferred over derive); derive \
is the general case that needs no such metric to be defined. If the question follows an earlier \
answer in this conversation ("show that as a percentage", "and the change month to month"), \
derive works directly on the most recent result without querying again.

Answers
- Every number must come from a tool result in this conversation. Never guess. Simple arithmetic \
on returned numbers is fine.
- Do not add filters, date ranges or group_by the user did not ask for. "Total" means all time, one number.
- State only facts that appear in tool results. Do not add dates, currency symbols, sources or \
explanations of your own.
- An empty or null result means no matching rows; say so.
- Lead with the answer, then a few lines of context at most. NEVER list, enumerate or bullet the \
rows of a multi-row result (no "- April: 3,040.45" lines): the system attaches the full table \
automatically. Say only the takeaway: the highest, the lowest, the trend, the total. Show rates as percentages with one decimal and money with thousands separators \
and two decimals (no currency symbol).
- Name the definition you used in a few words (for example "net revenue = gross revenue minus \
refunds"). If the wording was ambiguous, say which meaning you chose.
- Earlier turns of the conversation are context for follow-ups ("and by month?", "what about refunds?"). \
Query again for anything new; figures from earlier answers may be repeated as they were.
- Today is {today}."""


def system_prompt() -> str:
    """Built fresh per call (not a module-level constant) so a settings change in a running
    process, e.g. in a test, is picked up without a restart."""
    with_sql = settings.ALLOW_RUN_SQL
    return _SYSTEM_PROMPT_TEMPLATE.format(
        tools=_TOOLS_WITH_SQL if with_sql else _TOOLS_NO_SQL,
        step_2=_STEP_2_WITH_SQL if with_sql else _STEP_2_NO_SQL,
        today=dt.date.today().isoformat())


def _visible(result: dict) -> dict:
    """What the model sees: everything except the audit-only keys."""
    return {k: v for k, v in result.items() if not k.startswith("_")}


def _describe_filters(filters) -> str:
    return "; ".join(f"{f['dimension']} {f.get('operator', '=')} {f['value']}" for f in filters)


def provenance(trace: list[dict]) -> str:
    """Footer built from what actually ran."""
    lines, seen = [], set()
    for step in trace:
        args, result = step["args"], step["result"]
        if "error" in result:
            continue
        if step["tool"] == "query_metric":
            parts = []
            for name in args.get("metrics", []):
                parts.append(f"{name} ({semantic.metric_summary(name).rstrip('.')})")
            line = "Governed metric: " + "; ".join(parts)
            if args.get("group_by"):
                line += f". Grouped by {', '.join(args['group_by'])}"
            if args.get("filters"):
                line += f". Filtered on {_describe_filters(args['filters'])}"
            if args.get("start_date") or args.get("end_date"):
                line += f". From {args.get('start_date', 'the start')} to {args.get('end_date', 'today')}"
        elif step["tool"] == "run_sql":
            line = ("Not a governed metric: ad-hoc read-only SQL, so definitions were applied by the "
                    f"assistant and are not guaranteed. Query: `{result['_sql']}`")
        else:
            continue
        if line not in seen:
            seen.add(line)
            lines.append(f"- {line}")
    if not lines:
        return ""
    return "\n\n---\nHow this was calculated\n" + "\n".join(lines)


def _ratio_metrics(trace: list[dict]) -> set[str]:
    """Column names to render as a percentage: governed ratio metrics, any column derive named
    _..._pct (its own output), and a derive _..._change of a column that was already a
    percentage (a change in refund_rate is percentage points, not a bare decimal)."""
    ratio = set()
    if any(s["tool"] == "query_metric" for s in trace):
        try:
            ratio |= semantic.ratio_metric_names()
        except Exception:  # noqa: BLE001  formatting only; never fail an answer over it
            pass
    for step in trace:
        if step["tool"] == "derive" and "error" not in step["result"]:
            for c in step["result"]["columns"]:
                if c.endswith("_pct") or (c.endswith("_change") and c[: -len("_change")] in ratio):
                    ratio.add(c)
    return ratio


def _finish(text: str, trace: list[dict], bad: list[str], think: bool, step_limit: bool) -> dict:
    parts = [text.strip()]
    ratio = _ratio_metrics(trace)
    table_text = tables.render(trace, ratio)
    if table_text:
        parts.append(table_text)
    if bad:
        parts.append("Warning: these figures are not in the query results and could not be "
                     f"verified: {', '.join(bad)}. Treat them with caution.")
    footer = provenance(trace)
    answer = "\n\n".join(p for p in parts if p) + footer
    # the pieces are returned separately as well, so an interface can lay them out itself
    return {"answer": answer, "llm_answer": text, "trace": trace, "ungrounded": bad,
            "step_limit": step_limit, "think": think, "tables_md": table_text,
            "footer_md": footer, "ratio_metrics": sorted(ratio)}


def _run(question: str, think: bool, history: list[dict] | None = None) -> dict:
    """`history`: earlier turns as [{"question": ..., "answer": ...}], oldest first."""
    history = history or []
    messages = [{"role": "system", "content": system_prompt()}]
    for turn in history:
        messages += [{"role": "user", "content": turn["question"]},
                     {"role": "assistant", "content": turn["answer"]}]
    messages.append({"role": "user", "content": question})
    # figures already stated (and grounded) in earlier answers may be repeated
    context = " ".join([question] + [t["answer"] for t in history])
    trace = []
    corrected = False

    for _ in range(MAX_STEPS):
        message = chat(messages, tools=TOOL_SPECS, think=think)
        messages.append(message)

        tool_calls = message.get("tool_calls")
        if not tool_calls:
            text = message.get("content", "")
            bad = grounding.ungrounded(text, context, trace)
            if bad and not corrected:
                # one automatic correction: the model may have multiplied or rounded a figure itself
                corrected = True
                messages.append({"role": "user", "content": (
                    f"These figures are not in any tool result: {', '.join(bad)}. Rewrite the "
                    "answer using only figures the tools returned (adding, subtracting or taking a "
                    "share of returned figures is fine), or say the figure is not available.")})
                continue
            return _finish(text, trace, bad, think, step_limit=False)

        for call in tool_calls:
            name = call["function"]["name"]
            args = call["function"]["arguments"]
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}

            impl = TOOL_IMPLS.get(name)
            if impl is None:
                result = {"error": f"Unknown tool {name!r}. Available: {', '.join(TOOL_IMPLS)}."}
            else:
                try:
                    # derive reads the trace to find the result to transform; not a model-supplied
                    # argument (TOOL_SPECS never exposes it), so it is injected here instead.
                    result = impl(**args, trace=trace) if name == "derive" else impl(**args)
                except TypeError as e:  # wrong or missing argument names
                    result = {"error": f"Bad arguments for {name}: {e}"}
                except Exception as e:
                    result = {"error": f"{name} failed: {' '.join(str(e).split())[:300]}"}

            trace.append({"tool": name, "args": args, "result": result})
            messages.append({"role": "tool", "tool_name": name,
                             "content": json.dumps(_visible(result), default=str)})

    return _finish("I could not finish answering within the step limit.", trace, [], think,
                   step_limit=True)


def escalation_reason(run: dict) -> str:
    """Why a fast answer should be re-run with thinking on; empty if it can stand."""
    trace = run["trace"]
    if any(s["tool"] == "run_sql" for s in trace):
        return "used ad-hoc SQL"
    if trace and "error" in trace[-1]["result"]:
        return "ended on a tool error"
    if run["ungrounded"]:
        return "unverified figures"
    if run["step_limit"]:
        return "step limit"
    return ""


def ask(question: str, history: list[dict] | None = None) -> dict:
    started, started_wall = time.monotonic(), time.time()  # monotonic excludes Mac sleep
    out = _run(question, think=settings.THINK, history=history)
    out["escalated"] = False
    reason = escalation_reason(out) if settings.ESCALATE and not settings.THINK else ""
    if reason:
        out = _run(question, think=True, history=history)
        out.update(escalated=True, escalation_reason=reason)
    out["elapsed_s"] = round(time.monotonic() - started, 1)  # awake time
    out["wall_s"] = round(time.time() - started_wall, 1)      # includes any sleep

    audit.log({"question": question, "turn": len(history or []) + 1, "model": settings.MODEL, "think": out["think"],
               "escalated": out["escalated"], "escalation_reason": out.get("escalation_reason"),
               "elapsed_s": out["elapsed_s"], "wall_s": out["wall_s"], "ungrounded": out["ungrounded"],
               "steps": audit.summarize_trace(out["trace"])})
    return out


if __name__ == "__main__":
    import sys

    q = sys.argv[1] if len(sys.argv) > 1 else "What is our net revenue?"
    out = ask(q)
    print(out["answer"])
    print("\ntools:", [s["tool"] for s in out["trace"]])
