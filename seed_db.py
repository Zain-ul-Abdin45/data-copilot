"""
Seeds the raw source tables (raw.customers / raw.orders / raw.payments) in
Postgres. Models are built by dbt itself (`dbt run`), not compiled here, so
the warehouse the agent queries is exactly what the dbt project defines.

    python seed_db.py && dbt run && dbt test
"""
import os
import random
from datetime import date, timedelta

import psycopg

STATUSES = ["placed", "shipped", "delivered", "cancelled", "refunded"]
METHODS = ["credit_card", "paypal", "bank_transfer", "gift_card"]

DSN = os.getenv("DATABASE_URL", "dbname=data_copilot")


def seed_raw_tables(conn: psycopg.Connection, n_customers=25, n_orders=120, seed=42):
    rng = random.Random(seed)  # deterministic, so eval expectations stay stable

    with conn.cursor() as cur:
        cur.execute("""
            drop schema if exists raw cascade;
            create schema raw;
            create table raw.customers (id integer primary key, first_name text, last_name text, email text);
            create table raw.orders (id integer primary key, customer_id integer, order_date date, status text);
            create table raw.payments (id integer primary key, order_id integer, payment_method text, amount numeric(10,2));
        """)

        for i in range(1, n_customers + 1):
            cur.execute(
                "insert into raw.customers values (%s,%s,%s,%s)",
                (i, f"First{i}", f"Last{i}", f"user{i}@example.com"),
            )

        payment_id = 1
        start = date(2026, 1, 1)
        for order_id in range(1, n_orders + 1):
            customer_id = rng.randint(1, n_customers)
            order_date = start + timedelta(days=rng.randint(0, 240))
            status = rng.choices(STATUSES, weights=[10, 25, 45, 10, 10])[0]
            cur.execute(
                "insert into raw.orders values (%s,%s,%s,%s)",
                (order_id, customer_id, order_date, status),
            )
            if status != "cancelled":
                cur.execute(
                    "insert into raw.payments values (%s,%s,%s,%s)",
                    (payment_id, order_id, rng.choice(METHODS), round(rng.uniform(15, 400), 2)),
                )
                payment_id += 1

    conn.commit()


if __name__ == "__main__":
    with psycopg.connect(DSN) as conn:
        seed_raw_tables(conn)
        for table in ("customers", "orders", "payments"):
            n = conn.execute(f"select count(*) from raw.{table}").fetchone()[0]
            print(f"raw.{table}: {n} rows")
