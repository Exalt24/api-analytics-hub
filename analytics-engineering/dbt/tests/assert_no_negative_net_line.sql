-- A line's net revenue (gross minus its share of the refunds) can reach zero and never go below it.
select order_line_id, line_gross_cents, allocated_refund_cents, line_net_cents
from {{ ref('fct_orders') }}
where line_net_cents < 0 or allocated_refund_cents < 0
