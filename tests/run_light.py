"""Fast tests only: no LLM, no database, no MetricFlow. Takes a couple of seconds.

    python tests/run_light.py
"""
import importlib.util
import sys
import time
import traceback
from pathlib import Path

here = Path(__file__).resolve().parent
started, passed, failed = time.time(), 0, 0
for path in sorted(here.glob("test_*.py")):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name in sorted(n for n in dir(module) if n.startswith("test_")):
        try:
            getattr(module, name)()
            passed += 1
        except Exception:  # noqa: BLE001
            failed += 1
            print(f"FAIL {path.stem}.{name}")
            traceback.print_exc(limit=4)
print(f"{passed} passed, {failed} failed in {time.time() - started:.1f}s")
sys.exit(1 if failed else 0)
