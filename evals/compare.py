"""
Side-by-side readout of eval result files, for the morning after a night run.

    python evals/compare.py evals/results/A.json evals/results/B.json [...]

Per case: passed runs / total, average seconds, how many runs were escalated.
Then the failure messages of each file, and the answers of rubric cases (a pass on
those only means the right tools ran; read them).
"""
import json
import sys
from pathlib import Path


def load(path: str) -> dict:
    report = json.loads(Path(path).read_text())
    report["_name"] = Path(path).stem
    return report


def cell(case: dict | None) -> str:
    if case is None or not case["runs"]:
        return "-"
    runs = case["runs"]
    avg = sum(r["elapsed_s"] for r in runs) / len(runs)
    esc = sum(r["escalated"] for r in runs)
    return f"{case['passed_runs']}/{len(runs)}  {avg:>4.0f}s" + (f" esc{esc}" if esc else "")


def main(paths: list[str]) -> int:
    if not paths:
        sys.exit(__doc__)
    reports = []
    for p in paths:
        if not Path(p).exists():
            print(f"MISSING: {p}\n  That run did not save any results (it was interrupted, or stopped "
                  f"before its first question). Look at the log next to it.\n")
        else:
            reports.append(load(p))
    if not reports:
        print("Nothing to compare.")
        return 1
    ids = list(dict.fromkeys(c["id"] for r in reports for c in r["cases"]))
    by_id = [{c["id"]: c for c in r["cases"]} for r in reports]

    print("Runs:")
    for r in reports:
        print(f"  {r['_name']}: agent={r.get('agent', 'agent:ask')} model={r['model']} "
              f"think={r['think']} escalate={r['escalate']} repeat={r['repeat']}")
    width = max((len(i) for i in ids), default=4)
    print("\n" + "case".ljust(width) + "  " + "  ".join(r["_name"][-26:].ljust(16) for r in reports))
    for cid in ids:
        print(cid.ljust(width) + "  " + "  ".join(cell(b.get(cid)).ljust(16) for b in by_id))

    for r, b in zip(reports, by_id):
        full = sum(c["passed_runs"] == len(c["runs"]) for c in r["cases"])
        print(f"\n== {r['_name']}: {full}/{len(r['cases'])} cases passed every run")
        for c in r["cases"]:
            bad = [f for run in c["runs"] for f in run["failures"]]
            if bad:
                print(f"  {c['id']}:")
                for f in dict.fromkeys(bad):
                    print(f"    - {f[:160]}")
    print("\n== Rubric cases: read these answers")
    for r in reports:
        for c in r["cases"]:
            if c.get("rubric") and c["runs"]:
                print(f"\n[{r['_name']}] {c['id']}: {' '.join(c['rubric'].split())}")
                print("  " + c["runs"][0]["answer"][:500].replace("\n", "\n  "))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
