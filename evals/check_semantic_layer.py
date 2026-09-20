"""
Checks that MetricFlow (the governed path) agrees with the independent
truth_sql in golden.yaml, with no LLM involved. Run this after any change to
the dbt semantic layer:

    python evals/check_semantic_layer.py

Always re-parses first: `mf query` reads target/semantic_manifest.json, which
is stale until `dbt parse` runs, so testing without it checks the old
definitions.
"""
import csv
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import psycopg
import yaml

from run_evals import DSN, close, truth_values

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / ".venv" / "bin"
DBT_DIR = ROOT.parent / "dbt-test-project"

# case id -> the metric request the agent is expected to make
REQUESTS = {
    "net_revenue_total": dict(metrics=["net_revenue"]),
    "gross_vs_net": dict(metrics=["gross_revenue", "refunded_revenue"]),
    "refund_rate": dict(metrics=["refund_rate"]),
    "cancellation_count": dict(metrics=["cancelled_order_count"]),
    "aov": dict(metrics=["average_order_value"]),
    "net_revenue_q2": dict(metrics=["net_revenue"], start="2026-04-01", end="2026-06-30"),
    "net_revenue_by_month": dict(metrics=["net_revenue"], group_by="metric_time__month",
                                 order="metric_time__month"),
    "orders_by_status": dict(metrics=["order_count"], group_by="order__status",
                             order="order__status"),
    "top_customers": dict(metrics=["net_revenue"], group_by="order__customer",
                          order="-net_revenue", limit=3),
}


def run_mf(req: dict) -> list[float]:
    """Metric cells of an mf query, row by row, in metric order."""
    with tempfile.NamedTemporaryFile(suffix=".csv") as out:
        cmd = [str(BIN / "mf"), "query", "--metrics", ",".join(req["metrics"]), "--csv", out.name]
        for flag, key in (("--group-by", "group_by"), ("--order", "order"), ("--limit", "limit"),
                          ("--start-time", "start"), ("--end-time", "end")):
            if key in req:
                cmd += [flag, str(req[key])]
        proc = subprocess.run(cmd, cwd=DBT_DIR, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(proc.stdout[-400:] + proc.stderr[-400:])
        rows = list(csv.DictReader(open(out.name)))
    values = []
    for row in rows:
        for m in req["metrics"]:
            values.append(float(row[m]) if row[m] != "" else float("nan"))
    return values


def check_glossary() -> int:
    """Every glossary term must point at a metric that exists."""
    manifest = json.loads((DBT_DIR / "target" / "semantic_manifest.json").read_text())
    metrics = {m["name"] for m in manifest["metrics"]}
    bad = 0
    for row in csv.DictReader(open(DBT_DIR / "seeds" / "glossary.csv")):
        if row["metric"] not in metrics:
            bad += 1
            print(f"[BAD GLOSSARY] {row['term']!r} -> unknown metric {row['metric']!r}")
    print(f"[{'OK' if not bad else 'FAIL'}] glossary terms all map to defined metrics")
    return bad


def main():
    subprocess.run([str(BIN / "dbt"), "parse"], cwd=DBT_DIR, check=True, capture_output=True)
    glossary_bad = check_glossary()

    cases = {c["id"]: c for c in
             yaml.safe_load((Path(__file__).parent / "golden.yaml").read_text())["cases"]}
    conn = psycopg.connect(DSN)
    conn.read_only = True

    bad = glossary_bad
    for cid, req in REQUESTS.items():
        expected = [v for _, v in truth_values(conn, cases[cid])]
        got = run_mf(req)
        # a percentage column is reported as a fraction by mf, so compare fractions
        ok = len(got) == len(expected) and all(close(g, e) for g, e in zip(got, expected))
        bad += not ok
        print(f"[{'OK' if ok else 'MISMATCH'}] {cid}")
        if not ok:
            print(f"    truth:      {[round(e, 4) for e in expected]}")
            print(f"    metricflow: {[round(g, 4) for g in got]}")
    print(f"\n{bad} problem(s)" if bad else f"\nall {len(REQUESTS)} governed cases agree with raw-SQL truth")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
