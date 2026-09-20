"""
"What exists" for the agent. Built from dbt's own manifest (descriptions) and
the database's information_schema (the columns that really exist), plus the
governed metrics, so it cannot drift from the warehouse. Replaces the old
hand-rolled YAML parser.

Search is keyword overlap. The catalog is small, and the tool contract
(query in, ranked entries out) stays the same if this becomes embedding
search later.
"""
import re

import db
import dbtproject
import settings

_STOP = {"the", "and", "for", "are", "our", "how", "what", "which", "does", "have", "has", "with",
         "this", "that", "from", "much", "many", "any", "you", "your", "can", "was", "were", "who",
         "when", "where", "why", "use", "used", "using", "data", "track", "tracks", "tracked",
         "table", "column", "field", "about", "know", "tell", "show", "there"}

_cache: dict = {"key": None, "entries": None}


def _stem(w: str) -> str:
    return w[:-1] if len(w) > 3 and w.endswith("s") else w  # payments -> payment


def _tokens(text: str) -> set[str]:
    return {_stem(w) for w in re.findall(r"[a-z0-9]+", text.lower())}


def relations() -> set[str]:
    """Tables and views the agent's role can query."""
    _, rows = db.query("select table_name from information_schema.tables where table_schema = %s",
                       (settings.SCHEMA,))
    return {r[0] for r in rows}


def _build() -> list[dict]:
    manifest = dbtproject.load(dbtproject.MANIFEST)
    semantic = dbtproject.load(dbtproject.SEMANTIC_MANIFEST)

    docs = {}
    for node in manifest["nodes"].values():
        if node["resource_type"] in ("model", "seed"):
            path = node["original_file_path"]
            docs[node["name"]] = {
                "kind": node["resource_type"],
                "layer": path.split("/")[1] if path.startswith("models/") and path.count("/") > 1
                else node["resource_type"],
                "description": node.get("description", ""),
                "column_docs": {c: v.get("description", "") for c, v in node.get("columns", {}).items()},
            }

    joins: dict[str, list[str]] = {}
    for node in manifest["nodes"].values():
        meta = node.get("test_metadata") or {}
        if node["resource_type"] == "test" and meta.get("name") == "relationships":
            kw = meta["kwargs"]
            target = re.search(r"ref\('(\w+)'\)", kw["to"])
            table = (node.get("attached_node") or "").split(".")[-1]
            if target and table:
                joins.setdefault(table, []).append(
                    f"{table}.{kw['column_name']} = {target.group(1)}.{kw['field']}")

    cols, rows = db.query(
        "select table_name, column_name, data_type from information_schema.columns "
        "where table_schema = %s order by table_name, ordinal_position", (settings.SCHEMA,))
    columns: dict[str, list] = {}
    for table, col, typ in rows:
        columns.setdefault(table, []).append((col, typ))

    entries = []
    for table, cols_ in columns.items():
        if table.startswith("metricflow_"):  # MetricFlow plumbing, not business data
            continue
        d = docs.get(table, {"kind": "table", "layer": "", "description": "", "column_docs": {}})
        entries.append({
            "name": table, "kind": d["kind"], "layer": d["layer"], "description": d["description"],
            "queryable_with_run_sql": True,
            **({"join_keys": joins[table]} if table in joins else {}),
            "columns": [{"name": c, "type": t, **({"description": d["column_docs"][c]}
                                                  if d["column_docs"].get(c) else {})}
                        for c, t in cols_],
        })
    for m in semantic["metrics"]:
        entries.append({
            "name": m["name"], "kind": "governed metric", "layer": "semantic layer",
            "description": m.get("description", ""), "queryable_with_run_sql": False,
            "columns": [],
            "how_to_query": "query_metric (not run_sql)",
        })
    for e in entries:
        e["_tokens"] = _tokens(" ".join([e["name"].replace("_", " "), e["description"]] +
                                        [c["name"].replace("_", " ") + " " + c.get("description", "")
                                         for c in e["columns"]]))
    return entries


def _entries() -> list[dict]:
    dbtproject.ensure_fresh()
    key = dbtproject.MANIFEST.stat().st_mtime
    if _cache["key"] != key:
        _cache.update(key=key, entries=_build())
    return _cache["entries"]


def search_catalog(query: str, limit: int = 6) -> dict:
    terms = {t for t in _tokens(query) if len(t) > 2 and t not in _STOP}
    scored = []
    for e in _entries():
        score = len(terms & e["_tokens"])
        if score:
            scored.append((score, e))
    scored.sort(key=lambda x: (-x[0], x[1]["name"]))
    top = scored[:limit]
    matches = [{k: v for k, v in e.items() if not k.startswith("_")} for _, e in top]
    if not matches:
        return {"matches": [], "note": "Nothing in the catalog matches, so this data does not "
                                       "exist in the warehouse."}
    covered = set().union(*(terms & e["_tokens"] for _, e in top))
    result = {"matches": matches}
    if terms - covered:
        # e.g. "delivery" in "average delivery time": weak matches on the other words
        # must not be mistaken for the thing that was asked about
        result["terms_with_no_match"] = sorted(terms - covered)
        result["note"] = ("Some words in the question match nothing in the catalog. If they name "
                          "the thing being asked about, that data does not exist.")
    return result
