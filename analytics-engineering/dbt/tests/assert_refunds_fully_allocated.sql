-- Every cent refunded must land on a line: per order, the allocated refunds over its lines must equal the refunds in the source, and no order may be
-- refunded for more than it was worth. Returns the orders that break either rule.
with allocated as (
    select order_id, sum(allocated_refund_cents) as allocated_cents, max(order_gross_cents) as order_gross_cents
    from {{ ref('fct_orders') }}
    group by order_id
),

source_refunds as (
    select order_id, sum(amount_cents) as refunded_cents
    from {{ ref('stg_shop__refunds') }}
    group by order_id
)

select a.order_id, a.allocated_cents, coalesce(r.refunded_cents, 0) as refunded_cents, a.order_gross_cents
from allocated as a
left join source_refunds as r on r.order_id = a.order_id
where a.allocated_cents <> coalesce(r.refunded_cents, 0)
   or coalesce(r.refunded_cents, 0) > a.order_gross_cents
