"""
"What exists" for the agent. Built from dbt's own manifest (descriptions) and
the database's information_schema (the columns that really exist), plus the
governed metrics, so it cannot drift from the warehouse. Replaces the old
hand-rolled YAML parser.

Search is keyword overlap; matching itself is decided by that alone, never by embeddings (see
_ranked's docstring for why). settings.CATALOG_EMBEDDINGS (off by default) adds a cosine-based
re-rank on top, among entries keyword overlap already matched.
"""
import re

import datasources
import db
import dbtproject
import llm
import privacy
import settings

_STOP = {"the", "and", "for", "are", "our", "how", "what", "which", "does", "have", "has", "with",
         "this", "that", "from", "much", "many", "any", "you", "your", "can", "was", "were", "who",
         "when", "where", "why", "use", "used", "using", "data", "track", "tracks", "tracked",
         "table", "column", "field", "about", "know", "tell", "show", "there"}

# One wording for "the thing asked about is not in the warehouse", used by every engine's catalog
NOT_FOUND_NOTE = ("Nothing in the warehouse (no table, column or metric) mentions: {terms}. If those "
                  "words name what the user asked about, tell the user plainly that this data is not "
                  "available in the warehouse. Do not answer with related metrics as if they covered it.")

_cache: dict = {"key": None, "entries": None}


def _stem(w: str) -> str:
    return w[:-1] if len(w) > 3 and w.endswith("s") else w  # payments -> payment


def _tokens(text: str) -> set[str]:
    return {_stem(w) for w in re.findall(r"[a-z0-9]+", text.lower())}


def tokens(text: str) -> set[str]:
    """Lower-cased word stems (payments -> payment) of any text."""
    return _tokens(text)


def content_terms(text: str) -> set[str]:
    """The meaningful words of a question: stems, without short or filler words."""
    return {t for t in _tokens(text) if len(t) > 2 and t not in _STOP}


def relations() -> set[str]:
    """Tables and views the agent's role can query."""
    return {table for table, _, _ in datasources.get().columns()}


def table_columns() -> dict[str, list[tuple[str, str]]]:
    """table -> [(column, data_type), ...], ordinal order, for every table the agent's role sees.
    Used by the UI's schema browser; not sent to the model (search_catalog has its own, separate
    entry shape)."""
    out: dict[str, list[tuple[str, str]]] = {}
    for table, column, dtype in datasources.get().columns():
        out.setdefault(table, []).append((column, dtype))
    return out


def column_info(table: str, column: str) -> dict:
    """Fill rate and a small sample of real values for one column, for the UI's schema browser
    (a person clicking "(i)" next to a column, not the model). Refuses anything not already in
    the real catalog — table/column names here must never be arbitrary text, since this builds
    SQL directly rather than going through sqlguard, which exists specifically to validate
    MODEL-written SQL, not this. A column marked pii: true (privacy.py) never returns sample
    VALUES, only the fill rate, which reveals a count, not content."""
    cols = table_columns()
    if table not in cols or column not in {c for c, _ in cols[table]}:
        return {"error": f"{table}.{column} is not a real column in this warehouse."}
    ds = datasources.get()
    qualified = ds.qualify(table)
    row = db.query_dicts(f"select count(*) as total, count({column}) as filled from {qualified}")[0]
    total, filled = row["total"], row["filled"]
    fill_pct = round(100 * filled / total, 1) if total else 0.0
    if column.lower() in privacy.pii_column_names():
        return {"fill_pct": fill_pct, "masked": True}
    sample_rows = db.query_dicts(
        f"select distinct {column} as v from {qualified} where {column} is not null limit 5")
    return {"fill_pct": fill_pct, "sample": [db.jsonable(r["v"]) for r in sample_rows]}


def _metric_tables(semantic_manifest: dict) -> dict[str, set[str]]:
    """metric name -> underlying table(s), resolved from a simple metric's measure through
    derived/ratio metrics that reference other metrics, down to each measure's semantic model.
    Lets a table-level focus (e.g. from the UI's scope selector) reach the metrics built on it,
    without a metric having to be tagged with its table by hand."""
    measure_table = {}
    for sm in semantic_manifest["semantic_models"]:
        table = (sm.get("node_relation") or {}).get("alias")
        if table:
            for measure in sm["measures"]:
                measure_table[measure["name"]] = table

    by_name = {m["name"]: m for m in semantic_manifest["metrics"]}
    resolved: dict[str, set[str]] = {}

    def resolve(name: str, seen: frozenset) -> set[str]:
        if name in resolved:
            return resolved[name]
        if name in seen:  # a defensive guard; metric definitions should never cycle
            return set()
        metric = by_name.get(name)
        if metric is None:
            return set()
        p, seen = metric["type_params"], seen | {name}
        if metric["type"] == "simple":
            measure = p.get("measure")
            tables = {measure_table[measure["name"]]} if measure and measure["name"] in measure_table else set()
        elif metric["type"] == "derived":
            tables = set().union(set(), *(resolve(ref["name"], seen) for ref in p.get("metrics", [])))
        elif metric["type"] == "ratio":
            tables = resolve(p["numerator"]["name"], seen) | resolve(p["denominator"]["name"], seen)
        else:
            tables = set()
        resolved[name] = tables
        return tables

    return {name: resolve(name, frozenset()) for name in by_name}


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
                "pii": {c for c, v in node.get("columns", {}).items() if (v.get("meta") or {}).get("pii")},
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

    columns: dict[str, list] = {}
    for table, col, typ in datasources.get().columns():
        columns.setdefault(table, []).append((col, typ))

    entries = []
    for table, cols_ in columns.items():
        if table.startswith("metricflow_"):  # MetricFlow plumbing, not business data
            continue
        d = docs.get(table, {"kind": "table", "layer": "", "description": "", "column_docs": {}, "pii": set()})
        entries.append({
            "name": table, "kind": d["kind"], "layer": d["layer"], "description": d["description"],
            "queryable_with_run_sql": True,
            **({"join_keys": joins[table]} if table in joins else {}),
            "columns": [{"name": c, "type": t,
                         **({"description": d["column_docs"][c]} if d["column_docs"].get(c) else {}),
                         **({"pii": True, "note": "personal data: hidden in every tool result, not just here"}
                            if c in d["pii"] else {})}
                        for c, t in cols_],
        })
    metric_tables = _metric_tables(semantic)
    for m in semantic["metrics"]:
        entries.append({
            "name": m["name"], "kind": "governed metric", "layer": "semantic layer",
            "description": m.get("description", ""), "queryable_with_run_sql": False,
            "columns": [],
            "how_to_query": "query_metric (not run_sql)",
            "tables": sorted(metric_tables.get(m["name"], set())),
        })
    texts = []
    for e in entries:
        text = " ".join([e["name"].replace("_", " "), e["description"]] +
                        [c["name"].replace("_", " ") + " " + c.get("description", "")
                         for c in e["columns"]])
        e["_tokens"] = _tokens(text)
        texts.append(text)
    if settings.CATALOG_EMBEDDINGS:
        # One batched call for the whole catalog, computed once per cache build (dbt manifest
        # change), not per search. None (server/model unreachable) just means no entry gets an
        # "_embedding" — _ranked then has nothing to re-rank with, same as the feature being off.
        vecs = llm.embed(["search_document: " + t for t in texts])
        if vecs:
            for e, v in zip(entries, vecs):
                e["_embedding"] = v
    return entries


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na, nb = sum(x * x for x in a) ** 0.5, sum(x * x for x in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def _entries() -> list[dict]:
    dbtproject.ensure_fresh()
    key = dbtproject.MANIFEST.stat().st_mtime
    if _cache["key"] != key:
        _cache.update(key=key, entries=_build())
    return _cache["entries"]


def _in_focus(entry: dict, focus: set[str]) -> bool:
    """A table entry is in focus by its own name; a metric entry is in focus if it is built on a
    focused table (e.g. focusing out fct_orders also deprioritizes net_revenue, refund_rate...),
    resolved by _metric_tables from the dbt manifest, not a list maintained by hand."""
    return entry["name"] in focus or bool(set(entry.get("tables", ())) & focus)


def _ranked(entries: list[dict], terms: set[str], focus: set[str] | None,
           query_vec: list[float] | None = None) -> list[tuple[float, dict]]:
    """Entries that match `terms` at all, ranked by score first, same as with no focus at all: a
    focused table never jumps ahead of one that genuinely matches the question better. `focus`
    (table/metric names, e.g. from a UI scope selector) only breaks a tie between equally-good
    matches in favour of what the user is looking at. Soft, not a restriction: see
    sqlguard/relations() for the actual security boundary, which this has no part in.

    `query_vec` (settings.CATALOG_EMBEDDINGS) only nudges the ORDER among entries keyword
    overlap already matched — it can never pull in an entry with zero keyword overlap. A raw
    cosine similarity was measured, live, against this catalog's actual entries: an unrelated
    query ("weather forecast tomorrow") scored higher against one real entry than a genuinely
    relevant query scored against its best match, too thin a margin to trust for deciding
    whether something matches at all without weakening the "not available" guarantee
    (NOT_FOUND_NOTE) that depends on keyword overlap being the gate."""
    scored = []
    for e in entries:
        score: float = len(terms & e["_tokens"])
        if score and query_vec is not None and e.get("_embedding"):
            score += 0.5 * _cosine(query_vec, e["_embedding"])
        if score:
            scored.append((score, e))
    focus = focus or set()
    scored.sort(key=lambda x: (-x[0], not _in_focus(x[1], focus), x[1]["name"]))
    return scored


def search_catalog(query: str, limit: int = 6, focus: set[str] | None = None) -> dict:
    terms = {t for t in _tokens(query) if len(t) > 2 and t not in _STOP}
    query_vec = None
    if settings.CATALOG_EMBEDDINGS:
        vecs = llm.embed(["search_query: " + query])
        query_vec = vecs[0] if vecs else None
    scored = _ranked(_entries(), terms, focus, query_vec)
    top = scored[:limit]
    matches = [{k: v for k, v in e.items() if not k.startswith("_")} for _, e in top]
    if not matches:
        return {"matches": [], "note": NOT_FOUND_NOTE.format(terms=", ".join(sorted(terms)) or "the question")}
    covered = set().union(*(terms & e["_tokens"] for _, e in top))
    result = {"matches": matches}
    if terms - covered:
        # e.g. "delivery" in "average delivery time": weak matches on the other words
        # must not be mistaken for the thing that was asked about
        result["terms_with_no_match"] = sorted(terms - covered)
        result["note"] = NOT_FOUND_NOTE.format(terms=", ".join(result["terms_with_no_match"]))
    return result
