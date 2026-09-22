"""
Writes ui/preview/charts.html: the interface's charts drawn from REAL query results (no model),
in light and dark, so they can be checked by eye in a browser.

    .venv/bin/python ui/preview.py && open ui/preview/charts.html
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import render  # noqa: E402
import semantic  # noqa: E402


def step(result: dict) -> dict:
    return {"tool": "query_metric", "args": {}, "result": result}


def main() -> None:
    ratio = semantic.ratio_metric_names()
    traces = {
        "Net revenue and refund rate by month (two units: two charts)": [step(semantic.query_metric(
            ["net_revenue", "refund_rate"], group_by=["metric_time__month"], order_by=["metric_time__month"]))],
        "Orders by status (categories: bars)": [step(semantic.query_metric(
            ["order_count"], group_by=["order__status"], order_by=["order__status"]))],
        "Top customers by net revenue (ids labelled)": [step(semantic.query_metric(
            ["net_revenue"], group_by=["order__customer"], order_by=["-net_revenue"], limit=3))],
        "Gross vs refunded revenue by month (two series: legend)": [step(semantic.query_metric(
            ["gross_revenue", "refunded_revenue"], group_by=["metric_time__month"], order_by=["metric_time__month"]))],
    }
    sections, first = [], True
    for theme in ("light", "dark"):
        t = render.THEMES[theme]
        body = [f'<div style="background:{t["surface"]};color:{t["ink"]};padding:24px;margin-bottom:24px;'
                f'font-family:system-ui"><h2>{theme.title()} theme</h2>']
        for title, trace in traces.items():
            specs = render.chart_specs(trace, ratio)
            body.append(f"<h4>{title}</h4>" + ("" if specs else "<p>(no chart: a table is the better form)</p>"))
            for spec in specs:
                fig = render.make_figure(spec, theme)
                body.append(fig.to_html(full_html=False, include_plotlyjs=True if first else False))
                first = False
        body.append("</div>")
        sections.append("".join(body))
    out = HERE / "preview" / "charts.html"
    out.parent.mkdir(exist_ok=True)
    out.write_text("<html><head><meta charset='utf-8'><title>Chart preview</title></head><body style='margin:0'>"
                   + "".join(sections) + "</body></html>")
    print(f"wrote {out} ({out.stat().st_size // 1024} KB); open it in a browser")


if __name__ == "__main__":
    main()
