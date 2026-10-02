-- Net revenue is gross minus refunds, so a status marked as a refund must also
-- count in gross revenue. Otherwise refunds would be subtracted but never added.
select status
from {{ ref('order_status_rules') }}
where is_refund and not counts_in_gross_revenue
