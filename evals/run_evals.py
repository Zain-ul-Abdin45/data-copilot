"""
Runs golden.yaml against the copilot.

    python evals/run_evals.py --truth      # only compute expected values (no LLM)
    python evals/run_evals.py              # run every case through the agent
    python evals/run_evals.py -k refund    # only case ids containing "refund"
    python evals/run_evals.py --repeat 3 --pause 15   # 3 runs per case, 15 s rest between questions
    python evals/run_evals.py --agent ../wren-bakeoff/agent_wren.py:ask   # another engine, same questions

Results are written to evals/results/<timestamp>.json after every question, so a
run that is interrupted still leaves something to read. To run it gently in the
background, use evals/night.sh.

Grading is deterministic where it can be (numbers in the answer, tools in the
trace, required phrases). Cases with a `rubric` are printed for human or
LLM-judge review rather than auto-graded.
"""
import argparse
import datetime as dt
import json
import os
import re
import sys
import time
from pathlib import Path

import psycopg
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DSN = os.getenv("DATABASE_URL", "dbname=data_copilot")
NUM_RE = re.compile(r"-?\d[\d,]*\.?\d*")
TRUTH_PREFIX = yaml.safe_load((Path(__file__).parent / "golden.yaml").read_text())["truth_prefix"]


def truth_values(conn, case) -> list[float]:
    """Every numeric cell of truth_sql, plus x100 for pct_columns."""
    if "truth_sql" not in case:
        return []
    sql = case["truth_sql"]
    if re.search(r"\bfrom t\b", sql):  # uses the shared per-order CTE
        sql = TRUTH_PREFIX + sql
    with conn.cursor() as cur:
        cur.execute(sql)
        cols = [d.name for d in cur.description]
        rows = cur.fetchall()
    pct = set(case.get("pct_columns", []))
    values = []
    for row in rows:
        for col, v in zip(cols, row):
            if v is None:
                continue
            values.append((col in pct, float(v)))
    return values


def numbers_in(text: str) -> list[float]:
    out = []
    for m in NUM_RE.findall(text):
        try:
            out.append(float(m.replace(",", "").rstrip(".")))
        except ValueError:
            pass
    return out


def close(a: float, b: float) -> bool:
    return abs(a - b) <= max(0.01, abs(b) * 0.005)


def grade(case, answer: str, trace: list[dict], truth) -> list[str]:
    """Return a list of failure strings (empty = pass)."""
    failures = []
    used = [t["tool"] for t in trace]
    succeeded = [t["tool"] for t in trace if "error" not in t["result"]]
    for tool in case.get("tools_required", []):
        if tool not in succeeded:  # a call that returned an error did not use the tool
            failures.append(f"required tool did not succeed: {tool}")
    for tool in case.get("tools_forbidden", []):
        if tool in used:
            failures.append(f"forbidden tool called: {tool}")
    for phrase in case.get("must_mention", []):
        if phrase.lower() not in answer.lower():
            failures.append(f"answer does not mention: {phrase!r}")

    found = numbers_in(answer)
    for is_pct, v in truth:
        candidates = [v, v * 100] if is_pct else [v]
        if not any(close(f, c) for f in found for c in candidates):
            failures.append(f"expected value missing from answer: {v:g}")
    return failures


def load_agent(spec: str):
    """'agent:ask' (a module) or 'path/to/file.py:ask'."""
    target, func = spec.rsplit(":", 1)
    if target.endswith(".py"):
        import importlib.util
        path = Path(target).resolve()
        mod_spec = importlib.util.spec_from_file_location(path.stem, path)
        module = importlib.util.module_from_spec(mod_spec)
        mod_spec.loader.exec_module(module)
    else:
        import importlib
        module = importlib.import_module(target)
    return getattr(module, func)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--truth", action="store_true", help="only print expected values")
    ap.add_argument("-k", default="", help="substring filter on case id")
    ap.add_argument("--repeat", type=int, default=1, help="runs per case; a case passes only if all pass")
    ap.add_argument("--pause", type=float, default=0, help="seconds to rest between questions")
    ap.add_argument("--out", default=None, help="results file (default evals/results/<timestamp>.json)")
    ap.add_argument("--skip-model-check", action="store_true",
                    help="do not check that Ollama and the model are available first")
    ap.add_argument("--resume", default=None, metavar="RESULTS.json",
                    help="continue an interrupted run: keep its finished runs, do only the rest "
                         "(same agent, model, --repeat and settings are required)")
    ap.add_argument("--agent", default="agent:ask",
                    help="module-or-file.py:function that answers a question (default agent:ask)")
    args = ap.parse_args()

    cases = yaml.safe_load((Path(__file__).parent / "golden.yaml").read_text())["cases"]
    cases = [c for c in cases if args.k in c["id"]]

    conn = psycopg.connect(DSN)
    conn.read_only = True  # truth queries must never change data

    if args.truth:
        for c in cases:
            vals = [round(v, 4) for _, v in truth_values(conn, c)]
            print(f"{c['id']:<28} {c['category']:<11} {vals}")
        return

    import settings
    if not args.skip_model_check:
        import llm
        problem = llm.ready()
        if problem:
            sys.exit(f"Not starting: {problem}")
    ask = load_agent(args.agent)  # imported late so --truth works without an LLM

    out_path = Path(args.out) if args.out else \
        Path(__file__).parent / "results" / (dt.datetime.now().strftime("%Y-%m-%d_%H%M")
                                             + ("" if args.agent == "agent:ask" else "_" + Path(args.agent.split(":")[0]).stem)
                                             + ".json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    report = {"agent": args.agent, "model": settings.MODEL, "think": settings.THINK, "escalate": settings.ESCALATE,
              "repeat": args.repeat, "cases": []}
    if args.resume:
        out_path = Path(args.resume)
        if not out_path.exists():
            sys.exit(f"Cannot resume: {out_path} does not exist.")
        saved = json.loads(out_path.read_text())
        differs = [f"{k}: saved {saved.get(k)!r}, now {report[k]!r}"
                   for k in ("agent", "model", "think", "escalate", "repeat") if saved.get(k) != report[k]]
        if differs:
            sys.exit("Cannot resume: this run's settings differ from the saved ones, and mixing them "
                     "would corrupt the comparison.\n  " + "\n  ".join(differs) +
                     "\nUse the same --agent and --repeat and the same COPILOT_* settings.")
        report = saved

    print(f"model={settings.MODEL} think={settings.THINK} escalate={settings.ESCALATE} "
          f"repeat={args.repeat} -> {out_path}", flush=True)
    if args.resume:
        done = [e["id"] for e in report["cases"] if len(e["runs"]) >= args.repeat]
        print(f"Resuming: {len(done)} case(s) already complete and kept; "
              f"{sum(len(e['runs']) for e in report['cases'])} run(s) saved so far.", flush=True)

    def save():
        out_path.write_text(json.dumps(report, indent=2, default=str))

    save()  # the results file exists from the start, even if nothing completes
    print(f"{len(cases)} cases x {args.repeat} run(s). The first question also loads the model, "
          "so expect about 40 s of silence; a line appears after every answer.", flush=True)
    try:
        for n, c in enumerate(cases, 1):
            entry = next((e for e in report["cases"] if e["id"] == c["id"]), None)
            if entry is None:
                entry = {"id": c["id"], "category": c["category"], "question": c["question"],
                         "rubric": c.get("rubric"), "passed_runs": 0, "runs": []}
                report["cases"].append(entry)
            runs = entry["runs"]
            if len(runs) >= args.repeat:
                print(f"=== [{n}/{len(cases)}] {c['id']}: already complete, skipped", flush=True)
                continue
            truth = truth_values(conn, c)
            for i in range(len(runs), args.repeat):  # a partly done case continues after its saved runs
                print(f"... [{n}/{len(cases)}] {c['id']} (run {i + 1}/{args.repeat})", flush=True)
                try:
                    result = ask(c["question"])
                except Exception as e:  # noqa: BLE001  one crash must not lose the rest of the night
                    failures = [f"agent raised {type(e).__name__}: {' '.join(str(e).split())[:200]}"]
                    runs.append({"failures": failures, "elapsed_s": 0, "escalated": False,
                                 "answer": "", "tools": []})
                    print(f"[FAIL] {c['id']} (run {i + 1}/{args.repeat}, crashed)\n        - {failures[0]}",
                          flush=True)
                else:
                    if c["category"] == "safety":
                        # truth_sql is a before/after check that data is unchanged, not a value to quote
                        failures = grade(c, result["answer"], result["trace"], [])
                        if truth_values(conn, c) != truth:
                            failures.append("data changed while answering a safety question")
                    else:
                        failures = grade(c, result["answer"], result["trace"], truth)
                    if result["ungrounded"]:
                        failures.append(f"unverified figures in the answer: {result['ungrounded']}")
                    wall = result.get("wall_s", result["elapsed_s"])
                    runs.append({"failures": failures, "elapsed_s": result["elapsed_s"], "wall_s": wall,
                                 "escalated": result["escalated"], "answer": result["answer"],
                                 "tools": [t["tool"] for t in result["trace"]]})
                    note = (f"{result['elapsed_s']}s" + (", escalated" if result["escalated"] else "") +
                            (f", Mac slept ~{(wall - result['elapsed_s']) / 60:.0f} min" if wall - result["elapsed_s"] > 60 else ""))
                    print(f"[{'FAIL' if failures else 'PASS'}] {c['id']} (run {i + 1}/{args.repeat}, {note})",
                          flush=True)
                    for f in failures:
                        print(f"        - {f}", flush=True)
                entry["passed_runs"] = sum(not r["failures"] for r in runs)
                save()  # after every run
                if args.pause and (i + 1 < args.repeat or n < len(cases)):
                    time.sleep(args.pause)

    except KeyboardInterrupt:
        save()
        print(f"\nInterrupted. Partial results are saved in {out_path}", flush=True)
        sys.exit(130)

    chosen = [e for e in report["cases"] if e["id"] in {c["id"] for c in cases}]
    total_pass = sum(len(e["runs"]) >= args.repeat and e["passed_runs"] == len(e["runs"]) for e in chosen)
    total_fail = len(chosen) - total_pass
    review = sum(bool(e.get("rubric")) for e in chosen)
    print(f"\n{total_pass} cases passed, {total_fail} failed"
          f"{f' (all {args.repeat} runs must pass)' if args.repeat > 1 else ''}; "
          f"{review} have a rubric to read in {out_path}", flush=True)
    sys.exit(1 if total_fail else 0)


if __name__ == "__main__":
    main()
