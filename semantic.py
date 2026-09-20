"""
The governed path: metrics defined in the dbt project, compiled to SQL by
MetricFlow. The model asks for a metric by name plus dimensions and filters;
it never writes SQL here, so "revenue" cannot mean two different things.
"""
import datetime as dt
import math
import os
import re

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

        # MetricFlow connects with whatever the dbt profile says: force the read-only role.
        os.environ.update(PGUSER=settings.DB_USER, PGPASSWORD=settings.DB_PASSWORD,
                          PGHOST=settings.DB_HOST)
        cfg = CLIConfiguration()
        cfg.setup(dbt_profiles_path=settings.DBT_DIR, dbt_project_path=settings.DBT_DIR)
        _cfg, _loaded_mtime = cfg, mtime
    return _cfg.mf


# ---- what exists -----------------------------------------------------------

def _rules() -> dict:
    return {
        "order_status_rules": db.query_dicts(
            "select status, counts_in_gross_revenue, is_refund, counts_as_net_order, rule "
            "from order_status_rules order by status"),
        "glossary": db.query_dicts("select term, metric, rule from glossary order by term"),
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


def _group_by_options(m) -> list[str]:
    names = sorted({d.dunder_name for d in m.dimensions if not d.dunder_name.startswith("metric_time")})
    return names + ["metric_time__day|week|month|quarter|year"]


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
        words = {w for w in q.split() if len(w) > 2}
        scored = []
        for m in metrics:
            hay = _norm(f"{m.name} {m.label} {m.description}")
            score = sum(1 for w in words if w in hay)
            if score:
                scored.append((score, m))
        chosen = [m for _, m in sorted(scored, key=lambda x: -x[0])[:5]]

    if not chosen:
        return {"metrics": [], "note": "No governed metric matches. Call describe_metrics with no "
                                       "term to list them all, or use search_catalog."}
    names = {m.name for m in chosen}
    return {
        "metrics": [{"name": m.name, "label": m.label, "description": m.description,
                     "type": m.type.value, "formula": _formula(m),
                     "group_by_options": _group_by_options(m)} for m in chosen],
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
    return {
        "governed": True,
        "columns": list(table.column_names),
        "rows": rows,
        "row_count": len(rows),
        "_sql": result.sql,  # audit trail only; not sent to the model
    }
