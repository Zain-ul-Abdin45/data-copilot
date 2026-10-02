"""
The governed path: metrics defined in the dbt project, compiled to SQL by
MetricFlow. The model asks for a metric by name plus dimensions and filters;
it never writes SQL here, so "revenue" cannot mean two different things.
"""
import datetime as dt
import math
import os
import re

import sqlglot
from sqlglot import exp

import datasources
import db
import dbtproject
import settings

_cfg = None
_loaded_mtime = 0.0


def _engine():
    """MetricFlow engine, rebuilt whenever the dbt project has been re-parsed."""
    global _cfg, _loaded_mtime
    dbtproject.ensure_fresh()
    mtime = dbtproject.SEMANTIC_MANIFEST.stat().st_mtime
    if _cfg is None or mtime != _loaded_mtime:
        from dbt_metricflow.cli.cli_configuration import CLIConfiguration

        # MetricFlow connects with whatever dbt profile target DBT_TARGET names: force the
        # active engine's own credentials, but only while it reads the profile. Left in
        # os.environ these would leak into every other connection and subprocess of this
        # process (e.g. the Postgres target would connect everywhere as copilot_ro).
        forced = {"DBT_TARGET": settings.DBT_TARGET, **datasources.get().dbt_env()}
        saved = {k: os.environ.get(k) for k in forced}
        os.environ.update(forced)
        try:
            cfg = CLIConfiguration()
            cfg.setup(dbt_profiles_path=settings.DBT_DIR, dbt_project_path=settings.DBT_DIR)
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        _cfg, _loaded_mtime = cfg, mtime
    return _cfg.mf


# ---- what exists -----------------------------------------------------------

def _rules() -> dict:
    ds = datasources.get()
    return {
        "order_status_rules": db.query_dicts(
            "select status, counts_in_gross_revenue, is_refund, counts_as_net_order, rule "
            f"from {ds.qualify('order_status_rules')} order by status"),
        "glossary": db.query_dicts(f"select term, metric, rule from {ds.qualify('glossary')} order by term"),
    }


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _formula(m) -> str:
    p = m.type_params
    kind = m.type.value
    if kind == "derived":
        return p.expr
    if kind == "ratio":
        return f"{p.numerator.name} / {p.denominator.name}"
    if kind == "simple" and p.measure:
        return f"sum of measure {p.measure.name} (which orders count is set by the business rules)"
    return kind


def defining_dimensions(metric_name: str) -> set[str]:
    """Dimensions a ratio metric is built from, e.g. refund_rate is built from order__status
    (its numerator counts orders with status = 'refunded'). Grouping or filtering such a metric
    by that dimension gives only 0% or 100% per group, which says nothing, so it is refused.
    Derived from the measure expressions, so new metrics are covered without a list to maintain."""
    manifest = dbtproject.load(dbtproject.SEMANTIC_MANIFEST)
    metric = next((m for m in manifest["metrics"] if m["name"] == metric_name), None)
    if metric is None or metric["type"] != "ratio":
        return set()
    wanted = {x["name"] for x in metric["type_params"].get("input_measures", [])}
    found: set[str] = set()
    for model in manifest["semantic_models"]:
        entity = model.get("primary_entity") or next(
            e["name"] for e in model["entities"] if e["type"] == "primary")
        dims = {(d.get("expr") or d["name"]): f"{entity}__{d['name']}"
                for d in model["dimensions"] if d["type"] == "categorical"}
        for measure in model["measures"]:
            if measure["name"] in wanted:
                columns = {c.name for c in sqlglot.parse_one(measure["expr"], read="postgres").find_all(exp.Column)}
                found |= {dims[c] for c in columns if c in dims}
    return found


_GRAINS = "day|week|month|quarter|year"


def _group_by_options(m) -> list[str]:
    """group_by names to offer for this metric. A TIME dimension (order__order_date,
    customer__first_order_date, ...) is offered with the same grain-choice template as
    metric_time, not bare: its dunder_name alone defaults to day grain with no ordering
    (query_metric only auto-orders a name ending in a grain suffix), which is how a trend
    question silently turned into hundreds of unordered daily rows instead of ~8 monthly ones."""
    blocked = defining_dimensions(m.name)
    options = set()
    for d in m.dimensions:
        if d.dunder_name.startswith("metric_time") or d.dunder_name in blocked:
            continue
        options.add(f"{d.dunder_name}__{_GRAINS}" if d.type.value == "time" else d.dunder_name)
    return sorted(options) + [f"metric_time__{_GRAINS}"]


def describe_metrics(term: str | None = None) -> dict:
    """List governed metrics, or explain the ones matching `term`, with the
    business rules and glossary that define them."""
    metrics = _engine().list_metrics()
    rules = _rules()

    if not term:
        return {
            "metrics": [{"name": m.name, "label": m.label, "description": m.description}
                        for m in sorted(metrics, key=lambda m: m.name)],
            "glossary": rules["glossary"],
        }

    q = _norm(term)
    exact = {g["metric"] for g in rules["glossary"] if _norm(g["term"]) == q}
    exact |= {m.name for m in metrics if _norm(m.name) == q or _norm(m.label or "") == q}
    if exact:
        chosen = [m for m in metrics if m.name in exact]
    else:
        # A metric must cover at least half of the question's meaningful words. One shared word
        # ("average" in "average delivery time") must not present average_order_value as an answer.
        import catalog

        words = catalog.content_terms(q)
        need = max(1, -(-len(words) // 2))
        scored = []
        for m in metrics:
            named = catalog.tokens(f"{m.name.replace('_', ' ')} {m.label}")
            hit = len(words & (named | catalog.tokens(m.description)))
            if words and hit >= need:
                # a word in the name or label counts double: "how much was refunded" must rank
                # refunded_revenue above metrics that only mention "refunded" in passing
                scored.append((hit + 2 * len(words & named), m))
        chosen = [m for _, m in sorted(scored, key=lambda x: -x[0])[:5]]

    if not chosen:
        from catalog import search_catalog

        # No metric is not the same as no data: check the tables and columns in the same call
        return {"metrics": [], "catalog_search": search_catalog(term),
                "note": (f"No governed metric matches {term!r}. That alone does not mean the data is "
                         "missing, so the catalog was searched too (catalog_search). If it also finds "
                         "nothing about what was asked, tell the user plainly that this data is not "
                         "available in the warehouse; do not stop at 'no governed metric'.")}
    names = {m.name for m in chosen}
    return {
        "metrics": [{"name": m.name, "label": m.label, "description": m.description,
                     "type": m.type.value, "formula": _formula(m),
                     "group_by_options": _group_by_options(m),
                     **({"not_groupable_by": sorted(defining_dimensions(m.name)),
                         "why": "this rate is defined over that dimension, so per-group values are only 0% or 100%"}
                        if defining_dimensions(m.name) else {})} for m in chosen],
        "glossary": [g for g in rules["glossary"] if g["metric"] in names or _norm(g["term"]) == q],
        "business_rules": rules["order_status_rules"],
    }


def ratio_metric_names() -> set[str]:
    """Metrics that are fractions, so tables can show them as percentages."""
    return {m.name for m in _engine().list_metrics() if m.type.value == "ratio"}


def metric_summary(name: str) -> str:
    """One-line definition, for the provenance footer."""
    for m in _engine().list_metrics():
        if m.name == name:
            return m.description or m.label or name
    return name


# ---- asking -----------------------------------------------------------------

_OPS = {"=", "!=", "<", ">", "<=", ">=", "in", "not in"}
_DIM = re.compile(r"^[a-z_]+__[a-z_]+$")
_TIME_GRAIN = re.compile(r"__(?:" + _GRAINS + r")$")


def _literal(v) -> str:
    if isinstance(v, bool):
        raise ValueError("boolean filter values are not supported")
    if isinstance(v, (int, float)):
        return repr(v)
    return "'" + str(v).replace("'", "''") + "'"


def _where(f: dict) -> str:
    dim, op = f.get("dimension", ""), str(f.get("operator", "=")).lower()
    if not _DIM.match(dim):
        raise ValueError(f"filter dimension {dim!r} must look like entity__dimension, "
                         f"e.g. order__status")
    if op not in _OPS:
        raise ValueError(f"filter operator must be one of {sorted(_OPS)}")
    val = f.get("value")
    if op in ("in", "not in"):
        if not isinstance(val, list) or not val:
            raise ValueError("'in' filters need a non-empty list value")
        rhs = "(" + ", ".join(_literal(x) for x in val) + ")"
    else:
        rhs = _literal(val)
    return f"{{{{ Dimension('{dim}') }}}} {op} {rhs}"


def _clean(v):
    v = db.jsonable(v)
    return None if isinstance(v, float) and math.isnan(v) else v


def query_metric(metrics: list[str], group_by: list[str] | None = None,
                 filters: list[dict] | None = None, start_date: str | None = None,
                 end_date: str | None = None, order_by: list[str] | None = None,
                 limit: int | None = None) -> dict:
    """Query governed metrics. Errors come back as text with suggestions so the
    model can correct itself."""
    if not order_by:
        # a trend must read left to right: MetricFlow returns buckets in no particular order.
        # Matches any time-grained group_by (metric_time__month, but equally order__order_date__month
        # or customer__first_order_date__week), not just the canonical metric_time alias — a model
        # asking for a trend on an entity-qualified date must still get it back in date order.
        order_by = [g for g in group_by or [] if _TIME_GRAIN.search(g)] or None
    asked = set(group_by or []) | {f.get("dimension") for f in filters or []}
    for name in metrics:
        clash = sorted(defining_dimensions(name) & asked)
        if clash:
            return {"error": (f"{name} is defined over {clash[0]} (its measures are built from it), so "
                              f"grouping or filtering it by {clash[0]} only gives 0% or 100% per group "
                              "and says nothing."),
                    "hint": (f"Ask for {name} without {clash[0]}. For a comparison, group it by a "
                             "different dimension such as time. For 'would X change the rate' "
                             "questions, get the overall value first.")}

    from metricflow.engine.metricflow_engine import MetricFlowQueryRequest

    try:
        request = MetricFlowQueryRequest.create(
            metric_names=list(metrics),
            group_by_names=list(group_by) if group_by else None,
            where_constraints=[_where(f) for f in filters] if filters else None,
            order_by_names=list(order_by) if order_by else None,
            limit=min(int(limit or settings.ROW_LIMIT), settings.ROW_LIMIT),
            time_constraint_start=dt.datetime.fromisoformat(start_date) if start_date else None,
            time_constraint_end=dt.datetime.fromisoformat(end_date) if end_date else None,
        )
        result = _engine().query(request)
    except Exception as e:  # MetricFlow raises InvalidQueryException with useful suggestions
        return {"error": re.sub(r"\s+", " ", str(e))[:700],
                "hint": "If the metric exists but the breakdown or filter you need is not an "
                        "available group_by, that is expected: it is not governed. Do not give up. "
                        "Call search_catalog to find the table holding it, then answer with "
                        "run_sql and say the result is not a governed metric."}

    table = result.result_df
    rows = [[_clean(v) for v in row] for row in table.rows]
    out = {
        "governed": True,
        "columns": list(table.column_names),
        "rows": rows,
        "row_count": len(rows),
        "_sql": result.sql,  # audit trail only; not sent to the model
    }
    if len(rows) >= settings.ROW_LIMIT:
        # a capped, unlabelled result invites exactly the failure this note exists to prevent:
        # summarising a truncated series as if it were the whole trend. Belt-and-braces alongside
        # _group_by_options steering away from day grain in the first place.
        out["note"] = (f"Result capped at {settings.ROW_LIMIT} rows; there may be more not shown. "
                        "For a trend question, prefer a coarser time grain (e.g. __month or "
                        "__quarter instead of __day) rather than summarising a truncated series.")
    return out
