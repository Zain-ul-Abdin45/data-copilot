select
    c.customer_id,
    c.first_name,
    c.last_name,
    c.email,
    count(o.order_id) as lifetime_orders,
    min(o.order_date) as first_order_date
from {{ ref('stg_customers') }} c
left join {{ ref('stg_orders') }} o
    on o.customer_id = c.customer_id
group by 1, 2, 3, 4
