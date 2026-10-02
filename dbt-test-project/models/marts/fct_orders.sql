select
    o.order_id,
    o.customer_id,
    o.order_date,
    o.status,
    r.counts_in_gross_revenue,
    r.is_refund,
    r.counts_as_net_order,
    sum(p.amount) as order_total
from {{ ref('stg_orders') }} o
left join {{ ref('order_status_rules') }} r
    on r.status = o.status
left join {{ ref('stg_payments') }} p
    on p.order_id = o.order_id
group by 1, 2, 3, 4, 5, 6, 7
