"""Thin facade over the active data source (datasources/), kept so callers written before the
multi-engine split (catalog.py's search, semantic.py's business-rules queries, tools.py's
run_sql) don't need to know or care which engine is configured."""
import datasources
from datasources.base import jsonable  # re-exported: existing callers import it from here

__all__ = ["query", "query_dicts", "jsonable"]


def query(sql: str, params=None) -> tuple[list[str], list[list]]:
    return datasources.get().query(sql, params or ())


def query_dicts(sql: str, params=None) -> list[dict]:
    return datasources.get().query_dicts(sql, params or ())
