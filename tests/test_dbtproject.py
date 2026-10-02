"""dbtproject.ensure_fresh(): when a compiled target/*.json is reused vs. re-parsed.
No LLM, no database, no real dbt process (subprocess.run is mocked)."""
import json
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dbtproject  # noqa: E402
import settings  # noqa: E402


def _write(target: Path, adapter_type: str | None) -> None:
    manifest = {"metadata": {"adapter_type": adapter_type}} if adapter_type else {}
    (target / "manifest.json").write_text(json.dumps(manifest))
    (target / "semantic_manifest.json").write_text("{}")


def _patched(dbt_dir: Path, target: Path, datasource: str, run_side_effect):
    return (
        mock.patch.object(settings, "DBT_DIR", dbt_dir),
        mock.patch.object(settings, "DATASOURCE", datasource),
        mock.patch.object(settings, "DBT_TARGET", datasource),
        mock.patch.object(dbtproject, "TARGET", target),
        mock.patch.object(dbtproject, "MANIFEST", target / "manifest.json"),
        mock.patch.object(dbtproject, "SEMANTIC_MANIFEST", target / "semantic_manifest.json"),
        mock.patch.object(dbtproject.datasources, "get", return_value=mock.Mock(dbt_env=lambda: {})),
        mock.patch("subprocess.run", side_effect=run_side_effect),
    )


def test_a_fresh_manifest_for_the_current_engine_is_reused_not_reparsed():
    with tempfile.TemporaryDirectory() as d:
        dbt_dir, target = Path(d), Path(d) / "target"
        target.mkdir()
        _write(target, "postgres")
        patches = _patched(dbt_dir, target, "postgres",
                           AssertionError("a matching engine must not trigger dbt parse"))
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7]:
            dbtproject.ensure_fresh()  # raising means the mock was called; no raise means it passed


def test_a_manifest_compiled_for_a_different_engine_forces_a_reparse():
    # the exact live bug: switching COPILOT_DATASOURCE from trino back to postgres with no model
    # file touched left target/ holding a trino-compiled manifest; MetricFlow then generated SQL
    # referencing a catalog postgres doesn't have ("cross-database references are not implemented")
    with tempfile.TemporaryDirectory() as d:
        dbt_dir, target = Path(d), Path(d) / "target"
        target.mkdir()
        _write(target, "trino")
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            return mock.Mock(returncode=0, stdout="", stderr="")

        patches = _patched(dbt_dir, target, "postgres", fake_run)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7]:
            dbtproject.ensure_fresh()
        assert calls, "a mismatched adapter_type must trigger dbt parse"


def test_a_manifest_with_no_metadata_at_all_is_treated_as_stale_not_a_crash():
    # an older or hand-built manifest.json (e.g. a trimmed test fixture) without "metadata" must
    # fail safe (re-parse) rather than raise a KeyError out of ensure_fresh
    with tempfile.TemporaryDirectory() as d:
        dbt_dir, target = Path(d), Path(d) / "target"
        target.mkdir()
        _write(target, None)
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            return mock.Mock(returncode=0, stdout="", stderr="")

        patches = _patched(dbt_dir, target, "postgres", fake_run)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7]:
            dbtproject.ensure_fresh()
        assert calls


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
