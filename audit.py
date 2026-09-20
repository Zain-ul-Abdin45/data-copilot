"""
Append-only audit log, one JSON line per question: which tools ran, with what
arguments and SQL, how many rows came back. It never stores result rows or the
answer text, because those can contain sensitive values.

Two uses:
  python audit.py recent [N]    the last N questions and how they were answered
  python audit.py fallbacks     questions answered with ad-hoc SQL, most frequent
                                tables first: the backlog of metrics worth governing

The interface is one function, log(), so it can be replaced by a shared audit
service later.
"""
import collections
import datetime as dt
import json
import sys

import settings


def summarize_trace(trace: list[dict]) -> list[dict]:
    steps = []
    for s in trace:
        r = s["result"]
        step = {"tool": s["tool"], "args": s["args"], "ok": "error" not in r}
        if not step["ok"]:
            step["error"] = str(r["error"])[:200]
        else:
            if "row_count" in r:
                step["row_count"] = r["row_count"]
                step["columns"] = r.get("columns")
            if "_sql" in r:
                step["sql"] = r["_sql"]
            if "_tables" in r:
                step["tables"] = r["_tables"]
        steps.append(step)
    return steps


def log(record: dict) -> None:
    """Never raises: a logging failure must not stop an answer being returned."""
    try:
        path = settings.AUDIT_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {"ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), **record}
        with path.open("a") as f:
            f.write(json.dumps(record, default=str) + "\n")
    except Exception as e:  # noqa: BLE001
        print(f"audit log failed: {e}", file=sys.stderr)


def read() -> list[dict]:
    if not settings.AUDIT_PATH.exists():
        return []
    return [json.loads(line) for line in settings.AUDIT_PATH.read_text().splitlines() if line.strip()]


def _cli(argv: list[str]) -> None:
    records = read()
    cmd = argv[0] if argv else "recent"
    if cmd == "recent":
        for r in records[-int(argv[1] if len(argv) > 1 else 10):]:
            tools = " > ".join(s["tool"] for s in r["steps"]) or "(no tools)"
            flags = ("escalated " if r.get("escalated") else "") + \
                    ("UNVERIFIED " if r.get("ungrounded") else "")
            print(f"{r['ts']}  {r['elapsed_s']:>5}s  {flags}{r['question']!r}\n    {tools}")
    elif cmd == "fallbacks":
        counts = collections.Counter()
        examples = collections.defaultdict(list)
        for r in records:
            for s in r["steps"]:
                if s["tool"] == "run_sql" and s["ok"]:
                    key = ", ".join(s.get("tables", [])) or "?"
                    counts[key] += 1
                    if r["question"] not in examples[key]:
                        examples[key].append(r["question"])
        for key, n in counts.most_common():
            print(f"{n:>3}  tables: {key}")
            for q in examples[key][:5]:
                print(f"       {q!r}")
        if not counts:
            print("No question has used ad-hoc SQL yet.")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    _cli(sys.argv[1:])
