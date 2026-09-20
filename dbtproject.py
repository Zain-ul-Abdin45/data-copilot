"""Keeps the agent's view of the dbt project fresh. MetricFlow and the catalog
read target/*.json, which is stale until `dbt parse` runs, so every read goes
through ensure_fresh() and re-parses when any model or YAML file is newer."""
import json
import os
import subprocess
import sys
from pathlib import Path

import settings

TARGET = settings.DBT_DIR / "target"
SEMANTIC_MANIFEST = TARGET / "semantic_manifest.json"
MANIFEST = TARGET / "manifest.json"


def _newest_source() -> float:
    files = [settings.DBT_DIR / "dbt_project.yml"]
    files += (settings.DBT_DIR / "models").rglob("*")
    return max((f.stat().st_mtime for f in files if f.is_file()), default=0.0)


def ensure_fresh() -> None:
    if SEMANTIC_MANIFEST.exists() and MANIFEST.exists() and \
            min(SEMANTIC_MANIFEST.stat().st_mtime, MANIFEST.stat().st_mtime) >= _newest_source():
        return
    dbt = Path(sys.executable).parent / "dbt"
    env = {**os.environ, "PGUSER": settings.DB_USER, "PGPASSWORD": settings.DB_PASSWORD,
           "PGHOST": settings.DB_HOST}
    proc = subprocess.run([str(dbt), "parse"], cwd=settings.DBT_DIR, env=env,
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"dbt parse failed:\n{(proc.stdout + proc.stderr)[-800:]}")


def load(path: Path) -> dict:
    ensure_fresh()
    return json.loads(path.read_text())
