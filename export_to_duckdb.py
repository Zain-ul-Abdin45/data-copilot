"""
Copies the raw source tables out of Postgres into a local DuckDB file, the way a lake's
ingestion step would land an OLTP table as a file. dbt then builds the same staging/mart
models and the same governed metrics on top of it — the point being that switching
COPILOT_DATASOURCE=duckdb changes nothing else.

    python export_to_duckdb.py
    cd dbt-test-project && DBT_TARGET=duckdb COPILOT_DUCKDB_PATH=.../warehouse.duckdb \\
        ../.venv/bin/dbt seed && ... dbt run && ... dbt test

Reads raw.* as the database owner (needs to see the raw schema, which copilot_ro cannot);
never touches the copilot_ro role or the analytics schema, both of which dbt owns.
"""
import os

import duckdb
import psycopg

import settings

PG_DSN = os.getenv("DATABASE_URL", "dbname=data_copilot")
TABLES = {
    "customers": "id integer, first_name varchar, last_name varchar, email varchar",
    "orders": "id integer, customer_id integer, order_date date, status varchar",
    "payments": "id integer, order_id integer, payment_method varchar, amount decimal(10,2)",
}


def export(pg_dsn: str = PG_DSN, duckdb_path: str = settings.DUCKDB_PATH) -> None:
    pg = psycopg.connect(pg_dsn)
    con = duckdb.connect(duckdb_path)  # read/write: this is the one process allowed to build it
    try:
        con.execute("create schema if not exists raw")
        for table, ddl in TABLES.items():
            with pg.cursor() as cur:
                cur.execute(f"select * from raw.{table}")
                cols = [d.name for d in cur.description]
                rows = cur.fetchall()
            con.execute(f"drop table if exists raw.{table}")
            con.execute(f"create table raw.{table} ({ddl})")
            placeholders = ", ".join("?" * len(cols))
            con.executemany(f"insert into raw.{table} values ({placeholders})", rows)
            print(f"raw.{table}: {len(rows)} rows")
    finally:
        pg.close()
        con.close()


if __name__ == "__main__":
    export()
    print(f"wrote {settings.DUCKDB_PATH}")
