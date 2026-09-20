"""
Everything that can be checked WITHOUT running the model, in one command.

    nice -n 15 python evals/preflight.py          # full, about a minute
    python evals/preflight.py --fast              # a few seconds: unit tests, database, Ollama, golden set

It does not generate a single token: Ollama is only asked which models are installed.
If this passes, a failure in the night run is the model's behaviour, not a broken
harness, missing service or crashed script. If an oracle check fails, the engine check
that pinpoints it is run automatically.
"""
import contextlib
import io
import json
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
WREN_DIR = ROOT.parent / "wren-bakeoff"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))

PY = sys.executable
CATEGORIES = {"metric", "fallback", "lookup", "absent", "open_ended", "safety"}


def sh(cmd: list[str], cwd: Path = ROOT, timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)


# ---- checks: each returns (ok, detail) --------------------------------------

def unit_tests():
    p = sh([PY, "tests/run_light.py"])
    return p.returncode == 0, (p.stdout.strip().splitlines() or ["no output"])[-1] + \
        ("" if p.returncode == 0 else "\n" + p.stdout[-800:] + p.stderr[-400:])


def postgres():
    import db

    problems = []
    try:
        for table, floor in (("fct_orders", 1), ("order_status_rules", 1), ("glossary", 1)):
            n = db.query(f"select count(*) from {table}")[1][0][0]
            if n < floor:
                problems.append(f"{table} is empty")
    except Exception as e:  # noqa: BLE001
        return False, f"cannot read analytics as the agent role: {e}"
    for label, sql in (("raw schema must be hidden", "select * from raw.orders limit 1"),
                       ("writes must be refused", "delete from analytics.fct_orders")):
        try:
            db.query(sql)
            problems.append(f"{label}, but the statement succeeded")
        except Exception:  # noqa: BLE001
            pass
    return not problems, "; ".join(problems) or "agent role reads analytics only, cannot write, cannot see raw"


def dbt_project():
    import dbtproject
    import settings

    dbtproject.ensure_fresh()
    dbt = Path(PY).parent / "dbt"
    p = sh([str(dbt), "test"], cwd=settings.DBT_DIR)
    done = [line for line in p.stdout.splitlines() if "Done. PASS" in line]
    ok = p.returncode == 0
    return ok, (done[-1].split("Done.")[-1].strip() if done else p.stdout[-400:]) + " (dbt tests)"


def glossary():
    import check_semantic_layer as c

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        bad = c.check_glossary()
    return bad == 0, buf.getvalue().strip() or "glossary maps to defined metrics"


def ollama():
    import llm
    import settings

    problem = llm.ready()
    return problem is None, problem or f"server up, {settings.MODEL} installed (nothing loaded)"


def golden():
    import psycopg
    import yaml

    from run_evals import DSN, truth_values
    from tools import TOOL_IMPLS

    cases = yaml.safe_load((HERE / "golden.yaml").read_text())["cases"]
    problems, ids = [], set()
    conn = psycopg.connect(DSN)
    conn.read_only = True
    for c in cases:
        cid = c.get("id", "?")
        if cid in ids:
            problems.append(f"duplicate id {cid}")
        ids.add(cid)
        if c.get("category") not in CATEGORIES:
            problems.append(f"{cid}: unknown category {c.get('category')!r}")
        if not c.get("question"):
            problems.append(f"{cid}: no question")
        for key in ("tools_required", "tools_forbidden"):
            for t in c.get(key, []):
                if t not in TOOL_IMPLS:
                    problems.append(f"{cid}: {key} names unknown tool {t!r}")
        if not all(isinstance(m, str) and m for m in c.get("must_mention", [])):
            problems.append(f"{cid}: bad must_mention")
        if "truth_sql" in c:
            try:
                if not truth_values(conn, c):
                    problems.append(f"{cid}: truth_sql returned no numbers")
            except Exception as e:  # noqa: BLE001
                problems.append(f"{cid}: truth_sql failed: {' '.join(str(e).split())[:100]}")
    return not problems, "; ".join(problems) or f"{len(cases)} cases consistent, every truth query runs"


def oracle(engine: str):
    if engine == "wren" and not WREN_DIR.exists():
        return True, "skipped: ../wren-bakeoff not found"
    p = sh([PY, "evals/oracle.py", "--engine", engine], timeout=600)
    line = next((ln for ln in p.stdout.splitlines() if ln.startswith("ORACLE_SUMMARY")), None)
    if line is None:
        return False, "oracle did not finish:\n" + (p.stdout + p.stderr)[-800:]
    summary = json.loads(line.split(" ", 1)[1])
    if p.returncode == 0:
        c = summary["counts"]
        return True, f"{c['OK']} ok, {c['XFAIL']} expected gaps (scripted model, real tools and loop)"
    # pinpoint: was it the engine or the harness?
    engine_check = [PY, "evals/check_semantic_layer.py"] if engine == "metricflow" else \
        [PY, str(WREN_DIR / "check_wren_cubes.py")]
    diag = sh(engine_check, timeout=600)
    return False, ("oracle failed: " + ", ".join(summary.get("bad", ["?"])) + "\n" +
                   "\n".join(ln for ln in p.stdout.splitlines() if not ln.startswith("ORACLE"))[-700:] +
                   f"\n--- engine check for diagnosis:\n{diag.stdout[-900:]}")


def runner_and_compare():
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        (d / "crash.py").write_text('def ask(q):\n    raise RuntimeError("simulated Ollama timeout")\n')
        (d / "good.py").write_text(
            "def ask(q):\n    t = 'The refund rate is 8.3%.'\n"
            "    return {'answer': t, 'llm_answer': t, 'escalated': False, 'elapsed_s': 0.1, 'ungrounded': [],\n"
            "            'trace': [{'tool': 'query_metric', 'args': {}, 'result': {'rows': [[0.0833]], 'row_count': 1}}]}\n")

        def run(stub, out):
            return sh([PY, "evals/run_evals.py", "-k", "refund_rate", "--repeat", "2",
                       "--skip-model-check", "--agent", f"{d / stub}:ask", "--out", str(d / out)])

        crash, good = run("crash.py", "crash.json"), run("good.py", "good.json")
        if crash.returncode != 1 or not (d / "crash.json").exists():
            return False, "a crashing agent must be reported, not abort the run:\n" + (crash.stdout + crash.stderr)[-500:]
        runs = json.loads((d / "crash.json").read_text())["cases"][0]["runs"]
        if len(runs) != 2 or not all("agent raised RuntimeError" in r["failures"][0] for r in runs):
            return False, f"crash was not recorded per run: {runs}"
        if good.returncode != 0 or json.loads((d / "good.json").read_text())["cases"][0]["passed_runs"] != 2:
            return False, "a correct stub agent did not pass:\n" + (good.stdout + good.stderr)[-500:]
        # Ctrl+C while the first question is being answered must keep a results file and exit 130
        marker = d / "started"
        (d / "slow.py").write_text(
            f"import pathlib, time\ndef ask(q):\n    pathlib.Path({str(marker)!r}).write_text('x')\n    time.sleep(120)\n")
        proc = subprocess.Popen([PY, "evals/run_evals.py", "-k", "refund_rate", "--repeat", "1",
                                 "--skip-model-check", "--agent", f"{d / 'slow.py'}:ask",
                                 "--out", str(d / "slow.json")], cwd=ROOT,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        deadline = time.time() + 30
        while not marker.exists() and time.time() < deadline and proc.poll() is None:
            time.sleep(0.2)
        if not marker.exists():
            proc.kill()
            return False, "the stub agent never started:\n" + proc.communicate()[0][-500:]
        proc.send_signal(signal.SIGINT)
        try:
            out = proc.communicate(timeout=30)[0]
        except subprocess.TimeoutExpired:
            proc.kill()
            return False, "run_evals ignored Ctrl+C"
        if proc.returncode != 130 or "Partial results are saved" not in out or not (d / "slow.json").exists():
            return False, f"Ctrl+C handling wrong (exit {proc.returncode}):\n{out[-500:]}"
        missing = sh([PY, "evals/compare.py", str(d / "slow.json"), str(d / "never_written.json")])
        if missing.returncode != 0 or "MISSING" not in missing.stdout:
            return False, "compare.py must report a missing file plainly:\n" + (missing.stdout + missing.stderr)[-500:]
        cmp_ = sh([PY, "evals/compare.py", str(d / "crash.json"), str(d / "good.json")])
        if cmp_.returncode != 0 or "refund_rate" not in cmp_.stdout or "agent raised" not in cmp_.stdout:
            return False, "compare.py could not read the results:\n" + (cmp_.stdout + cmp_.stderr)[-500:]
    return True, "a crashed question is recorded and the run continues; Ctrl+C keeps partial results; compare.py reads them and names missing files"


def resume_check():
    """--resume keeps finished runs, does only the rest, and refuses mismatched settings."""
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        counter, out = d / "calls", d / "r.json"
        (d / "stub.py").write_text(
            "import pathlib\n"
            f"C = pathlib.Path({str(counter)!r})\n"
            "def ask(q):\n"
            "    C.write_text((C.read_text() if C.exists() else '') + 'x')\n"
            "    t = 'The refund rate is 8.3%.'\n"
            "    return {'answer': t, 'llm_answer': t, 'escalated': False, 'elapsed_s': 0.1, 'ungrounded': [],\n"
            "            'trace': [{'tool': 'query_metric', 'args': {}, 'result': {'rows': [[0.0833]], 'row_count': 1}}]}\n")

        def run(*extra):
            return sh([PY, "evals/run_evals.py", "--skip-model-check", "--agent", f"{d / 'stub.py'}:ask",
                       "--out", str(out), "--repeat", "2", *extra])

        def calls():
            return len(counter.read_text()) if counter.exists() else 0

        run("-k", "net_revenue_total")                                    # 1 case x 2 runs
        if calls() != 2:
            return False, f"setup: expected 2 calls, got {calls()}"
        r = sh([PY, "evals/run_evals.py", "--skip-model-check", "--agent", f"{d / 'stub.py'}:ask",
                "--resume", str(out), "--repeat", "2", "-k", "net_revenue"])
        cases = json.loads(out.read_text())["cases"]
        if calls() != 2 + 4 or [c["id"] for c in cases][0] != "net_revenue_total" or len(cases) != 3:
            return False, f"resume must skip the finished case and run the 2 new ones ({calls()} calls):\n" + r.stdout[-400:]
        if "already complete, skipped" not in r.stdout:
            return False, "resume did not say it skipped the finished case"

        data = json.loads(out.read_text())                                # simulate an interrupt mid-case
        data["cases"][1]["runs"] = data["cases"][1]["runs"][:1]
        data["cases"][1]["runs"][0]["answer"] = "KEPT FROM BEFORE"
        out.write_text(json.dumps(data))
        before = calls()
        sh([PY, "evals/run_evals.py", "--skip-model-check", "--agent", f"{d / 'stub.py'}:ask",
            "--resume", str(out), "--repeat", "2", "-k", "net_revenue"])
        runs = json.loads(out.read_text())["cases"][1]["runs"]
        if calls() - before != 1 or len(runs) != 2 or runs[0]["answer"] != "KEPT FROM BEFORE":
            return False, f"a half-done case must continue after its saved run ({calls() - before} calls)"

        bad = sh([PY, "evals/run_evals.py", "--skip-model-check", "--agent", f"{d / 'stub.py'}:ask",
                  "--resume", str(out), "--repeat", "1", "-k", "net_revenue"])
        if bad.returncode == 0 or "Cannot resume" not in (bad.stdout + bad.stderr):
            return False, "mismatched --repeat must be refused"
    return True, "finished runs kept, only missing ones run, a half-done case continues, mismatched settings refused"


def night_scripts():
    problems = []
    for name in ("night.sh", "night_all.sh"):
        path = HERE / name
        if not path.exists():
            problems.append(f"{name} missing")
        elif sh(["sh", "-n", str(path)]).returncode != 0:
            problems.append(f"{name} has a syntax error")
    results = HERE / "results"
    try:
        results.mkdir(exist_ok=True)
        probe = results / ".write_test"
        probe.write_text("x")
        probe.unlink()
    except OSError as e:
        problems.append(f"cannot write to {results}: {e}")
    free_gb = shutil.disk_usage(ROOT).free / 1e9
    if free_gb < 5:
        problems.append(f"only {free_gb:.0f} GB free")
    for tool in ("caffeinate", "nice"):
        if shutil.which(tool) is None:
            problems.append(f"{tool} not found")
    if WREN_DIR.exists() and not (WREN_DIR / ".venv" / "bin" / "wren").exists():
        problems.append("wren CLI missing from ../wren-bakeoff/.venv")
    power = sh(["pmset", "-g", "batt"]).stdout if shutil.which("pmset") else ""
    warning = ("\nWARNING: on battery. Plug in the charger before a long run; closing the lid "
               "sleeps the Mac even under caffeinate, so keep it open.") if "Battery Power" in power else ""
    return not problems, ("; ".join(problems) or
                          f"scripts parse, results dir writable, {free_gb:.0f} GB free") + warning


CHECKS = [  # (name, function, in --fast)
    ("unit tests (stubbed, no LLM)", unit_tests, True),
    ("postgres: agent role scope", postgres, True),
    ("ollama: server and model present", ollama, True),
    ("golden.yaml consistent, truth queries run", golden, True),
    ("night scripts, results dir, disk", night_scripts, True),
    ("dbt project fresh, dbt tests", dbt_project, False),
    ("glossary maps to defined metrics", glossary, False),
    ("oracle: MetricFlow (real tools, scripted model)", lambda: oracle("metricflow"), False),
    ("oracle: Wren (real tools, scripted model)", lambda: oracle("wren"), False),
    ("eval runner survives crashes; compare.py", runner_and_compare, False),
    ("eval runner --resume", resume_check, False),
]


def main() -> int:
    fast = "--fast" in sys.argv
    started, failed = time.time(), 0
    for name, fn, in_fast in CHECKS:
        if fast and not in_fast:
            continue
        t = time.time()
        try:
            ok, detail = fn()
        except Exception as e:  # noqa: BLE001
            ok, detail = False, f"{type(e).__name__}: {e}"
        failed += not ok
        print(f"[{' OK ' if ok else 'FAIL'}] {name}  ({time.time() - t:.0f}s)", flush=True)
        for line in detail.splitlines():
            print(f"        {line}", flush=True)
    print(f"\n{'PREFLIGHT PASSED' if not failed else f'{failed} CHECK(S) FAILED'} "
          f"in {time.time() - started:.0f}s" + (" (fast mode)" if fast else ""), flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
