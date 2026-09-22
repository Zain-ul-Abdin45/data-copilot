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


def tool_contracts():
    """The real tools, no model: the guards and wording the agent depends on."""
    import semantic
    from catalog import search_catalog

    import os

    problems = []
    env_before = {k: os.environ.get(k) for k in ("PGUSER", "PGPASSWORD", "PGHOST")}
    described = semantic.describe_metrics("refund rate")["metrics"][0]
    if "order__status" in described["group_by_options"] or described.get("not_groupable_by") != ["order__status"]:
        problems.append("describe_metrics still offers order__status for refund_rate")
    if "error" not in semantic.query_metric(["refund_rate"], group_by=["order__status"]):
        problems.append("refund_rate by status was not refused")
    overall = semantic.query_metric(["refund_rate"])
    if "error" in overall or abs(overall["rows"][0][0] - 0.0833) > 0.001:
        problems.append(f"overall refund_rate wrong: {overall}")
    by_status = semantic.query_metric(["net_revenue"], group_by=["order__status"])
    if "error" in by_status or by_status["row_count"] != 5:
        problems.append("net_revenue by status must still work")
    if {k: os.environ.get(k) for k in env_before} != env_before:
        problems.append("starting the MetricFlow engine changed PG* environment variables (they leak "
                        "into other connections and subprocesses)")
    unmatched = semantic.describe_metrics("average delivery time")
    inner = unmatched.get("catalog_search", {})
    if unmatched["metrics"] or "not available" not in inner.get("note", "") or "delivery" not in inner.get("terms_with_no_match", []):
        problems.append("describe_metrics on an absent topic must include a catalog search that says "
                        f"the data is not available: {str(unmatched)[:150]}")
    missing = search_catalog("average delivery time in days")
    if "delivery" not in missing.get("terms_with_no_match", []) or "not available" not in missing.get("note", ""):
        problems.append(f"an absent topic must be reported as not available: {missing.get('note')}")
    return not problems, "; ".join(problems) or (
        "rates refuse their own dimension, other metrics still group, an absent topic is reported as unavailable")


import contextlib


@contextlib.contextmanager
def _serve_ui():
    """The interface (stub agent, no model) on a free port; yields (port, page_html)."""
    import os
    import socket
    import urllib.request

    ui = ROOT / "ui"
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    proc = subprocess.Popen([str(Path(PY).parent / "chainlit"), "run", "app.py", "--host", "127.0.0.1",
                             "--port", str(port), "--headless"], cwd=ui,
                            env={**os.environ, "COPILOT_UI_STUB": "1"},
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        page, deadline = None, time.time() + 60
        while time.time() < deadline and proc.poll() is None:
            try:
                page = urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=3).read().decode()
                break
            except OSError:
                time.sleep(1)
        if page is None:
            proc.terminate()
            raise RuntimeError("the interface did not start:\n" + (proc.communicate(timeout=10)[0] or "")[-800:])
        yield port, page
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def ui_server():
    """The interface starts, serves its page and settings, and its page makes no third-party
    requests. Uses the stub agent, so no model is involved."""
    import urllib.request

    ui = ROOT / "ui"
    if not (ui / "app.py").exists():
        return True, "skipped: no ui/ folder"
    sh([PY, str(ui / "harden.py")])
    if sh([PY, str(ui / "harden.py"), "--check"]).returncode != 0:
        return False, "Chainlit's page still loads third-party hosts (Google Fonts / jsDelivr)"
    with _serve_ui() as (port, page):
        external = [h for h in ("fonts.googleapis.com", "fonts.gstatic.com", "cdn.jsdelivr.net") if h in page]
        if external:
            return False, f"the served page still references {external}"
        conf = json.loads(urllib.request.urlopen(
            f"http://127.0.0.1:{port}/project/settings?language=en-US", timeout=5).read())
        starters = conf.get("starters") or conf.get("ui", {}).get("starters") or []
    return True, f"starts on a free port, serves its page and settings ({len(starters)} starters), no third-party requests"


def ui_conversation():
    """Two messages through the real Chainlit runtime over its socket, the way the browser sends
    them: answer, charts and side panel arrive, no errors, and the second turn sees the first."""
    import asyncio
    import datetime as dt
    import uuid

    import socketio

    if not (ROOT / "ui" / "app.py").exists():
        return True, "skipped: no ui/ folder"

    async def talk(port):
        sio, events = socketio.AsyncClient(), []

        @sio.on("*")
        async def collect(event, *args):
            events.append((event, args[0] if args else None))

        await sio.connect(f"http://127.0.0.1:{port}", socketio_path="/ws/socket.io", transports=["websocket"],
                          auth={"sessionId": str(uuid.uuid4()), "userEnv": "{}", "clientType": "webapp",
                                "chatProfile": "", "threadId": ""})
        await sio.emit("connection_successful")
        await asyncio.sleep(1)
        for text in ("Show net revenue by month.", "And the refund rate?"):
            done = sum(e == "task_end" for e, _ in events)
            await sio.emit("client_message", {"message": {
                "id": str(uuid.uuid4()), "name": "User", "type": "user_message", "output": text,
                "createdAt": dt.datetime.now(dt.timezone.utc).isoformat()}, "fileReferences": []})
            for _ in range(60):
                await asyncio.sleep(0.5)
                if sum(e == "task_end" for e, _ in events) > done and any(
                        e == "new_message" and "stub" in json.dumps(a, default=str) for e, a in events[-12:]):
                    break
        await sio.disconnect()
        return events

    with _serve_ui() as (port, _):
        events = asyncio.run(talk(port))
    blob = [(e, json.dumps(a, default=str)) for e, a in events]
    problems = []
    if any('"isError": true' in b for _, b in blob) or any(e == "error" for e, _ in blob):
        problems.append("an error event was sent to the browser")
    answers = [b for e, b in blob if e == "new_message" and "stub, turn" in b]
    if len(answers) != 2 or "**Governed metric**" not in answers[0]:
        problems.append(f"expected two answers starting with the governed badge, got {len(answers)}")
    elif "stub, turn 2" not in answers[1]:
        problems.append("the second turn did not receive the first (history is not carried)")
    plotly = [b for e, b in blob if e == "element" and '"type": "plotly"' in b and '"display": "inline"' in b]
    side = [b for e, b in blob if e == "element" and "How this was calculated" in b and '"display": "side"' in b]
    if len(plotly) != 4 or len(side) != 2:
        problems.append(f"expected 4 inline charts and 2 side panels, got {len(plotly)} and {len(side)}")
    return not problems, "; ".join(problems) or (
        "two turns handled: badge, text, inline charts and side panel arrive, no errors, history carried")


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
    ("tool contracts: guards and wording (real tools)", tool_contracts, False),
    ("oracle: MetricFlow (real tools, scripted model)", lambda: oracle("metricflow"), False),
    ("oracle: Wren (real tools, scripted model)", lambda: oracle("wren"), False),
    ("eval runner survives crashes; compare.py", runner_and_compare, False),
    ("eval runner --resume", resume_check, False),
    ("interface starts (stub agent, no model)", ui_server, False),
    ("interface handles a conversation (stub agent)", ui_conversation, False),
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
