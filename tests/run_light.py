"""Fast tests only: no LLM, no database, no MetricFlow. Takes a couple of seconds.

    python tests/run_light.py

Reads target/manifest.json and target/semantic_manifest.json (via dbtproject.load) as a plain
JSON file read, no dbt process involved; DBT_PROJECT_DIR can point at the frozen fixture in
tests/fixtures/dbt_target instead of a real dbt project (see README's Tests section) — CI does
this, since dbt-test-project itself is a separate, unversioned sibling project, never checked
in anywhere.
"""
import importlib.util
import os
import sys
import time
import traceback
from pathlib import Path

here = Path(__file__).resolve().parent

# One test calls semantic.describe_metrics with nothing stubbed, which goes through the real
# MetricFlow engine (dbt_metricflow's own CLIConfiguration) — needing a full real dbt project
# (profiles.yml, a real `dbt parse`), not just the two manifest JSON files the fixture provides.
# Set by CI, which only has the fixture; unset for a local run against the real dbt-test-project,
# where this test runs as always.
_NEEDS_LIVE_ENGINE = {("test_semantic_guard", "test_metrics_named_after_the_asked_word_outrank_ones_that_only_mention_it")}
_skip_live_engine = bool(os.environ.get("COPILOT_LIGHT_TESTS_NO_LIVE_ENGINE"))

started, passed, failed, skipped = time.time(), 0, 0, 0
for path in sorted(here.glob("test_*.py")):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name in sorted(n for n in dir(module) if n.startswith("test_")):
        if _skip_live_engine and (path.stem, name) in _NEEDS_LIVE_ENGINE:
            skipped += 1
            print(f"SKIP {path.stem}.{name} (needs the real dbt project; COPILOT_LIGHT_TESTS_NO_LIVE_ENGINE set)")
            continue
        try:
            getattr(module, name)()
            passed += 1
        except Exception:  # noqa: BLE001
            failed += 1
            print(f"FAIL {path.stem}.{name}")
            traceback.print_exc(limit=4)
print(f"{passed} passed, {failed} failed, {skipped} skipped in {time.time() - started:.1f}s")
sys.exit(1 if failed else 0)
