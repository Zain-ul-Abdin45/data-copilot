"""
What the interface shows for one answer. Plain Python with no UI framework, so all of
it is testable without a browser. The screen has four parts:

  1. a badge saying where the numbers came from (governed metric, ad-hoc SQL, catalog)
  2. the model's words, then the tables drawn by code from the returned rows
  3. a chart, also drawn by code from the returned rows (never from the model's text)
  4. a "how this was calculated" panel: the definitions, the steps and the SQL

Chart rules follow the dataviz skill: a line for a trend, bars for magnitude, at most
three series from the validated palette, one axis (a different unit gets its own chart),
a legend only for two or more series, labels only at the extreme or the end (the table
carries every number), and separate light and dark colours.
"""
import datetime as dt
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import grounding  # noqa: E402
from tables import fmt_number, fmt_percent  # noqa: E402

THEMES = {  # series colours validated with the dataviz validator against each surface
    "light": {"surface": "#fcfcfb", "ink": "#0b0b0b", "ink2": "#52514e", "grid": "#ebebe8",
              "series": ["#2a78d6", "#eb6834", "#1baf7a"]},
    "dark": {"surface": "#1a1a19", "ink": "#ffffff", "ink2": "#c3c2b7", "grid": "#2e2e2c",
             "series": ["#3987e5", "#d95926", "#199e70"]},
}
MAX_CATEGORIES = 12   # more classes than this is a table, not a chart
MAX_TIME_POINTS = 60
MAX_SERIES = 3        # the first three palette slots validate for every chart form
MAX_TURNS = 4         # earlier turns handed back to the agent

_ID_LIKE = ("_id", "__customer", "__order")
_TIME_NAME = re.compile(r"(__(day|week|month|quarter|year)$|date|_at$)")
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}")


# ---- small helpers ----------------------------------------------------------

def humanize(column: str) -> str:
    """order__status -> Status, metric_time__month -> Month, net_revenue -> Net revenue."""
    text = column.rsplit("__", 1)[-1].replace("_", " ").strip()
    return text[:1].upper() + text[1:]


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _numeric_column(rows: list[list], i: int) -> bool:
    values = [r[i] for r in rows if r[i] is not None]
    return bool(values) and all(_is_num(v) for v in values)


def _id_like(name: str) -> bool:
    return name == "id" or name.endswith(_ID_LIKE)


def data_steps(trace: list[dict]) -> list[dict]:
    return [s for s in trace if s["tool"] in ("query_metric", "run_sql", "derive") and "error" not in s["result"]]


def classify(trace: list[dict]) -> tuple[str, str]:
    """(kind, label): where the numbers on screen came from. The weakest source wins."""
    steps = data_steps(trace)
    if any(s["tool"] == "run_sql" for s in steps):
        return "adhoc", "Ad-hoc SQL: not a governed metric"
    if steps:
        return "governed", "Governed metric"
    if any(s["tool"] in ("describe_metrics", "search_catalog") and "error" not in s["result"] for s in trace):
        return "catalog", "From the data catalog"
    return "none", ""


# ---- charts -----------------------------------------------------------------

def _time_labels(column: str, values: list) -> list[str]:
    grain = column.rsplit("__", 1)[-1] if "__" in column else "day"
    out = []
    for v in values:
        if not _ISO.match(str(v)):
            out.append(str(v))
            continue
        d = dt.date.fromisoformat(str(v)[:10])
        out.append({"month": d.strftime("%b %Y"), "year": d.strftime("%Y"),
                    "quarter": f"{d.year} Q{(d.month - 1) // 3 + 1}"}.get(grain, d.isoformat()))
    return out


def chart_specs(trace: list[dict], ratio: set[str] | frozenset = frozenset()) -> list[dict]:
    """Charts for the most recent multi-row result, at most two (one per unit: a rate and an
    amount never share an axis). Empty when a table is the better form."""
    for step in reversed(data_steps(trace)):
        result = step["result"]
        cols, rows = result["columns"], result["rows"]
        if len(rows) < 2:
            continue
        # the leading columns that are labels (text or ids), e.g. first name + last name
        label_cols = []
        for i, c in enumerate(cols):
            if _numeric_column(rows, i) and not _id_like(c):
                break
            label_cols.append(i)
        if not label_cols:
            continue
        ys = [i for i in range(len(cols)) if i not in label_cols and _numeric_column(rows, i) and not _id_like(cols[i])]
        if not ys:
            continue

        name = cols[label_cols[0]]
        is_time = len(label_cols) == 1 and (bool(_TIME_NAME.search(name)) or all(
            isinstance(r[label_cols[0]], str) and _ISO.match(r[label_cols[0]]) for r in rows))
        if len(rows) > (MAX_TIME_POINTS if is_time else MAX_CATEGORIES):
            continue
        if is_time:
            rows = sorted(rows, key=lambda r: str(r[label_cols[0]]))  # a trend reads left to right
            labels = _time_labels(name, [r[label_cols[0]] for r in rows])
            axis = humanize(name).lower()
        else:
            labels = [" ".join(f"{humanize(cols[i])} {r[i]}" if _id_like(cols[i]) else str(r[i])
                               for i in label_cols) for r in rows]
            prefixes = {cols[i].split("__")[0] for i in label_cols if "__" in cols[i]}
            axis = (prefixes.pop() if len(label_cols) > 1 and len(prefixes) == 1 else
                    " and ".join(humanize(cols[i]).lower() for i in label_cols))

        specs = []
        for pct in (False, True):
            idx = [i for i in ys if (cols[i] in ratio) == pct]
            if not idx or len(idx) > MAX_SERIES:
                continue  # more than three series: the table is the readable form
            kind = "line" if is_time and len(rows) >= 3 else "bar"
            specs.append({
                "kind": kind,
                "horizontal": kind == "bar" and not is_time and (len(rows) > 5 or max(map(len, labels)) > 12),
                "title": f"{', '.join(humanize(cols[i]) for i in idx)} by {axis}",
                "labels": labels,
                "pct": pct,
                "series": [{"name": humanize(cols[i]), "values": [r[i] for r in rows], "pct": pct}
                           for i in idx],
            })
        if specs:
            return specs
    return []


def _fmt(value: float, pct: bool) -> str:
    return fmt_percent(value) if pct else fmt_number(value)


def make_figure(spec: dict, theme: str = "light"):
    """A Plotly figure for a chart spec. Imported lazily so the rest works without Plotly."""
    import plotly.graph_objects as go

    t = THEMES[theme]
    labels, n = spec["labels"], len(spec["labels"])
    multi = len(spec["series"]) > 1
    horizontal = spec["horizontal"]
    fig = go.Figure()

    for i, s in enumerate(spec["series"]):
        color, values = t["series"][i], s["values"]
        fmt = ".1%" if s["pct"] else ",.2f"
        known = [(j, v) for j, v in enumerate(values) if v is not None]
        # one label only: the end of a line, the largest bar (the table carries every number)
        pick = known[-1][0] if spec["kind"] == "line" and known else max(known, key=lambda p: p[1])[0] if known else None
        text = [_fmt(v, s["pct"]) if j == pick and v is not None else "" for j, v in enumerate(values)]

        if spec["kind"] == "line":
            fig.add_trace(go.Scatter(
                x=labels, y=values, name=s["name"], mode="lines+markers+text", text=text,
                textposition="top center", textfont=dict(color=t["ink"], size=12),
                line=dict(color=color, width=2),
                marker=dict(size=8, color=color, line=dict(width=2, color=t["surface"])),
                hovertemplate=f"%{{x}}<br>{s['name']}: %{{y:{fmt}}}<extra></extra>"))
        else:
            fig.add_trace(go.Bar(
                x=values if horizontal else labels, y=labels if horizontal else values,
                orientation="h" if horizontal else "v", name=s["name"], text=text,
                textposition="outside", textfont=dict(color=t["ink"], size=12), cliponaxis=False,
                marker=dict(color=color, cornerradius=4),
                hovertemplate=(f"%{{y}}<br>{s['name']}: %{{x:{fmt}}}<extra></extra>" if horizontal
                               else f"%{{x}}<br>{s['name']}: %{{y:{fmt}}}<extra></extra>")))

    value_axis = dict(gridcolor=t["grid"], gridwidth=1, zeroline=False, showline=False,
                      tickfont=dict(color=t["ink2"]), tickformat=".0%" if spec["pct"] else ",.0f",
                      rangemode="tozero")
    label_axis = dict(showgrid=False, showline=True, linecolor=t["grid"], tickfont=dict(color=t["ink2"]),
                      type="category")
    if horizontal:
        label_axis["autorange"] = "reversed"  # first row at the top, like the table
    band = (46 if horizontal else 560 / max(n, 1))
    height = 90 + 46 * n if horizontal else 340
    fig.update_layout(
        title=dict(text=spec["title"], x=0, font=dict(size=14, color=t["ink"])),
        paper_bgcolor=t["surface"], plot_bgcolor=t["surface"], font=dict(color=t["ink2"]),
        height=height, margin=dict(l=16, r=32, t=56, b=40), showlegend=multi,
        legend=dict(orientation="h", y=1.12, x=0, font=dict(color=t["ink2"])),
        barmode="group", bargap=1 - min(24, band * 0.6) / band, bargroupgap=0.08,
        hoverlabel=dict(bgcolor=t["surface"], bordercolor=t["grid"], font=dict(color=t["ink"])),
        xaxis=value_axis if horizontal else label_axis, yaxis=label_axis if horizontal else value_axis)
    return fig


# ---- the answer as shown ----------------------------------------------------

def _step_line(i: int, step: dict) -> list[str]:
    args = {k: v for k, v in step["args"].items() if v not in (None, [], "")}
    result = step["result"]
    shown = json.dumps(args, default=str)
    if len(shown) > 160:
        shown = shown[:157] + "..."
    if "error" in result:
        return [f"{i}. **{step['tool']}** {shown} -> refused or failed: {str(result['error'])[:160]}"]
    lines = [f"{i}. **{step['tool']}** {shown}"
             + (f" -> {result['row_count']} row(s)" if "row_count" in result else "")]
    if result.get("_sql"):
        lines += ["", "   ```sql", "   " + result["_sql"].strip().replace("\n", "\n   "), "   ```"]
    return lines


def details_md(result: dict) -> str:
    footer = [ln[2:] for ln in result.get("footer_md", "").splitlines() if ln.startswith("- ")]
    lines = ["#### How this was calculated", ""] + [f"- {f}" for f in footer] if footer else \
        ["#### How this was calculated", "", "- No data query was run for this answer."]
    if result["trace"]:
        lines += ["", "#### Steps", ""]
        for i, step in enumerate(result["trace"], 1):
            lines += _step_line(i, step)
    lines += ["", f"_{result['elapsed_s']:.0f} s_" + (" (re-checked with deeper reasoning)" if result.get("escalated") else "")]
    return "\n".join(lines)


_LIST_ITEM = re.compile(r"^\s*(?:[-*\u2022]|\d+[.)])\s+")


def strip_listing(text: str, trace: list[dict]) -> str:
    """The model sometimes restates a multi-row result as a list right above the table that shows
    it. Drop a run of 3+ short list lines when every figure in them is in the returned rows, so
    nothing is lost and the table stays the single place the numbers appear."""
    cells: set[float] = set()
    for step in data_steps(trace):
        if len(step["result"]["rows"]) >= 2:
            cells = {float(v) for row in step["result"]["rows"] for v in row if _is_num(v)}
            # A fraction reported as a percent: not bounded to +/-1, since a period-over-period
            # growth metric can legitimately exceed 100% (net_revenue_growth_mom of 1.66 = "166%").
            cells |= {c * 100 for c in cells}
    if not cells:
        return text

    def redundant(line: str) -> bool:
        figures = grounding.stated_figures(line)
        return len(line) <= 60 and bool(figures) and all(grounding.matches(f, cells) for f in figures)

    lines, out, i = text.split("\n"), [], 0
    while i < len(lines):
        j = i
        while j < len(lines) and _LIST_ITEM.match(lines[j]):
            j += 1
        if j - i >= 3 and all(redundant(line) for line in lines[i:j]):
            if not any(ln.strip() for ln in lines[j:]):
                # the list was the last thing in the text: the sentence that introduced it (if it
                # ends with a colon, e.g. "... is as follows:") must not dangle with nothing after
                # it once the list itself is gone. Left alone otherwise: a colon followed by
                # unrelated content that survives the strip is not dangling.
                for k in range(len(out) - 1, -1, -1):
                    if out[k].strip():
                        if out[k].rstrip().endswith(":"):
                            out[k] = out[k].rstrip()[:-1] + "."
                        break
            i = j
            continue
        out.extend(lines[i:max(j, i + 1)])
        i = max(j, i + 1)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def build_view(result: dict) -> dict:
    kind, label = classify(result["trace"])
    notes = []
    if result.get("escalated"):
        notes.append(f"Checked again with deeper reasoning: the first pass {result['escalation_reason']}.")
    if result.get("step_limit"):
        notes.append("Stopped at the step limit before finishing.")
    warning = ("These figures are not in the query results and could not be verified: "
               + ", ".join(result["ungrounded"]) + ". Treat them with caution.") if result["ungrounded"] else ""
    text = strip_listing(result["llm_answer"].strip(), result["trace"]) if result.get("tables_md") \
        else result["llm_answer"].strip()
    return {"kind": kind, "label": label, "text": text,
            "tables_md": result.get("tables_md", ""), "warning": warning, "notes": notes,
            "charts": chart_specs(result["trace"], set(result.get("ratio_metrics", []))),
            "details_md": details_md(result)}


def compose(view: dict) -> str:
    parts = [f"**{view['label']}**"] if view["label"] else []
    parts.append(view["text"])
    if view["tables_md"]:
        parts.append(view["tables_md"])
    if view["warning"]:
        parts.append(f"> **Warning.** {view['warning']}")
    parts += [f"> {n}" for n in view["notes"]]
    return "\n\n".join(p for p in parts if p)


def history_entry(question: str, result: dict) -> dict:
    """What the next turn needs: the question and the model's own words (not tables or footers)."""
    return {"question": question, "answer": result["llm_answer"].strip()}
