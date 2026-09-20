"""Read-only Postgres access. Safety comes from the database, not from the
prompt or from string checks: the role has SELECT on one schema, sessions are
read-only, and every statement has a timeout."""
import datetime as dt
from decimal import Decimal

import psycopg

import settings


def connect() -> psycopg.Connection:
    conn = psycopg.connect(
        host=settings.DB_HOST,
        dbname=settings.DB_NAME,
        user=settings.DB_USER,
        password=settings.DB_PASSWORD,
        options=(f"-c default_transaction_read_only=on "
                 f"-c statement_timeout={settings.STATEMENT_TIMEOUT_MS} "
                 f"-c search_path={settings.SCHEMA}"),
    )
    conn.read_only = True
    return conn


def jsonable(v):
    """Make a database value safe to hand to the model as JSON."""
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, dt.datetime):
        return v.date().isoformat() if v.time() == dt.time(0) else v.isoformat()
    if isinstance(v, dt.date):
        return v.isoformat()
    return v


def query(sql: str, params=None) -> tuple[list[str], list[list]]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [d.name for d in cur.description]
        return cols, [[jsonable(v) for v in row] for row in cur.fetchall()]


def query_dicts(sql: str, params=None) -> list[dict]:
    cols, rows = query(sql, params)
    return [dict(zip(cols, r)) for r in rows]
