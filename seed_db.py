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


def seed_raw_tables(conn: psycopg.Connection, n_customers=300, n_orders=3000, seed=42):
    """Realistic messiness, deliberately: a skewed customer order distribution (a few repeat
    customers, most placing one or two orders — enough for a customer-level metric to mean
    something), a few customers with no email (an incomplete signup, not a NULL bug), and a
    few orders with zero or two payments (a failed sync, a split payment). All three are
    already handled correctly upstream — the truth CTE's and fct_orders' sum(...) are both
    coalesced to 0 and sum a LEFT JOIN correctly with zero or several matching rows — so this
    is exercise, not a change to what "correct" means. The date range (Jan-Aug 2026) is
    unchanged: several golden.yaml cases assert on specific month labels."""
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
            email = None if rng.random() < 0.03 else f"user{i}@example.com"  # ~3%: signed up, no email on file
            cur.execute(
                "insert into raw.customers values (%s,%s,%s,%s)",
                (i, f"First{i}", f"Last{i}", email),
            )

        # A skewed but BOUNDED order count per customer (most order once or twice, a quarter are
        # repeat buyers, a few are loyal regulars) — capped 8:1 so no single customer can dominate
        # the way an unbounded heavy-tailed draw (Pareto, tried first) did: one customer landed
        # 2,101 of 3,000 orders, a data error to design against, not the realism aimed for.
        weights = rng.choices([1, 3, 8], weights=[70, 25, 5], k=n_customers)
        customer_ids = list(range(1, n_customers + 1))

        payment_id = 1
        start = date(2026, 1, 1)
        for order_id in range(1, n_orders + 1):
            customer_id = rng.choices(customer_ids, weights=weights)[0]
            order_date = start + timedelta(days=rng.randint(0, 240))
            status = rng.choices(STATUSES, weights=[10, 25, 45, 10, 10])[0]
            cur.execute(
                "insert into raw.orders values (%s,%s,%s,%s)",
                (order_id, customer_id, order_date, status),
            )
            if status == "cancelled":
                continue
            roll = rng.random()
            if roll < 0.02:
                continue  # ~2% of chargeable orders: no payment record at all (a sync failure)
            n_payments = 2 if roll < 0.07 else 1  # ~5%: split across two payment records
            for _ in range(n_payments):
                cur.execute(
                    "insert into raw.payments values (%s,%s,%s,%s)",
                    (payment_id, order_id, rng.choice(METHODS), round(rng.uniform(15, 400) / n_payments, 2)),
                )
                payment_id += 1

    conn.commit()


if __name__ == "__main__":
    with psycopg.connect(DSN) as conn:
        seed_raw_tables(conn)
        for table in ("customers", "orders", "payments"):
            n = conn.execute(f"select count(*) from raw.{table}").fetchone()[0]
            print(f"raw.{table}: {n} rows")
