# Business rules

Two CSV files hold the rules the business can change. No SQL or MetricFlow
knowledge is needed to edit them.

| File | What it controls |
|---|---|
| `order_status_rules.csv` | What each order status means for money: does it count in gross revenue, is it a refund, does it count as a net order. |
| `glossary.csv` | Which metric a word maps to (`revenue` -> `net_revenue`). `metric` must be defined in `models/marts/semantic_layer.yml`. |

The `rule` column is the plain-language version of each row. Keep it accurate,
because it is what users are shown when they ask how a number is calculated.

## After editing

```
cd dbt-test-project
dbt seed && dbt run && dbt test        # rebuild; tests reject inconsistent rules
python ../evals/check_semantic_layer.py   # metrics still match raw SQL
```

`dbt test` fails, and nothing should be published, if:
- a status used in the orders has no row in `order_status_rules.csv`;
- a status is marked `is_refund` but not `counts_in_gross_revenue`;
- a status or glossary term appears twice, or a flag is blank.

## What is not a CSV rule

The formulas stay in `models/marts/semantic_layer.yml`: net revenue is gross
minus refunds, and average order value is net revenue divided by net orders.
Rules that need new logic (for example "ignore test customers") are a dbt model
change, not a CSV row.
