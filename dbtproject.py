"""Keeps the agent's view of the dbt project fresh. MetricFlow and the catalog
read target/*.json, which is stale until `dbt parse` runs, so every read goes
through ensure_fresh() and re-parses when any model or YAML file is newer."""
import json
import os
import subprocess
import sys
from pathlib import Path

import datasources
import settings

TARGET = settings.DBT_DIR / "target"
SEMANTIC_MANIFEST = TARGET / "semantic_manifest.json"
MANIFEST = TARGET / "manifest.json"


def _newest_source() -> float:
    files = [settings.DBT_DIR / "dbt_project.yml"]
    files += (settings.DBT_DIR / "models").rglob("*")
    return max((f.stat().st_mtime for f in files if f.is_file()), default=0.0)


def ensure_fresh() -> None:
    """Re-parses when a model/YAML file is newer than the compiled manifest, OR when the
    manifest was built for a different engine: COPILOT_DATASOURCE switched since the last dbt
    parse, with no model file touched to trip the mtime check above, so target/ still holds a
    manifest compiled for the PREVIOUS engine's SQL dialect and catalog. Hit live: switching
    from trino back to postgres without an explicit dbt command reused a trino-compiled
    manifest, and MetricFlow then generated SQL referencing a catalog postgres doesn't have
    ("cross-database references are not implemented"). dbt's own manifest.json already records
    which adapter built it (metadata.adapter_type, one of dbt's own plugin names — the same
    strings as settings.DATASOURCE), so this needs no new state of its own."""
    fresh = SEMANTIC_MANIFEST.exists() and MANIFEST.exists() and \
        min(SEMANTIC_MANIFEST.stat().st_mtime, MANIFEST.stat().st_mtime) >= _newest_source()
    if fresh:
        try:
            fresh = json.loads(MANIFEST.read_text())["metadata"]["adapter_type"] == settings.DATASOURCE
        except Exception:
            fresh = False
    if fresh:
        return
    dbt = Path(sys.executable).parent / "dbt"
    env = {**os.environ, "DBT_TARGET": settings.DBT_TARGET, **datasources.get().dbt_env()}
    proc = subprocess.run([str(dbt), "parse"], cwd=settings.DBT_DIR, env=env,
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"dbt parse failed:\n{(proc.stdout + proc.stderr)[-800:]}")


def load(path: Path) -> dict:
    ensure_fresh()
    return json.loads(path.read_text())
