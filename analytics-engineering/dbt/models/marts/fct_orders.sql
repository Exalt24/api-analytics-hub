-- grain: one order line (one product on one order).
-- Refunds are booked against whole orders, so each order's refunded cents are spread over its lines in proportion to line value. The split uses the
-- running total of the lines in a window and rounds the running share, so the pieces add back to the refund to the cent (checked by
-- tests/assert_refunds_fully_allocated.sql). net revenue = gross minus allocated refund.
-- is_first_order_for_customer and days_since_prior_order come from windows over the customer's orders in time order.
with lines as (
    select
        l.order_line_id,
        l.order_id,
        l.product_id,
        l.quantity,
        l.unit_price_cents,
        l.line_gross_cents,
        o.customer_id,
        o.placed_at,
        o.order_date,
        o.financial_status,
        o.channel
    from {{ ref('stg_shop__order_lines') }} as l
    inner join {{ ref('stg_shop__orders') }} as o on o.order_id = l.order_id
),

refund_by_order as (
    select order_id, sum(amount_cents) as refunded_cents, max(refund_date) as last_refund_date
    from {{ ref('stg_shop__refunds') }}
    group by order_id
),

customer_orders as (
    select
        order_id,
        row_number() over (partition by customer_id order by placed_at, order_id) as customer_order_number,
        lag(order_date) over (partition by customer_id order by placed_at, order_id) as prior_order_date
    from {{ ref('stg_shop__orders') }}
),

running as (
    select
        lines.*,
        sum(line_gross_cents) over (partition by order_id) as order_gross_cents,
        sum(line_gross_cents) over (partition by order_id order by order_line_id rows between unbounded preceding and current row) as running_gross_cents
    from lines
),

allocated as (
    select
        running.*,
        coalesce(r.refunded_cents, 0) as order_refunded_cents,
        r.last_refund_date,
        {{ ae_intdiv('2 * coalesce(r.refunded_cents, 0) * running_gross_cents + order_gross_cents', '2 * order_gross_cents') }}
        - {{ ae_intdiv('2 * coalesce(r.refunded_cents, 0) * (running_gross_cents - line_gross_cents) + order_gross_cents', '2 * order_gross_cents') }} as allocated_refund_cents
    from running
    left join refund_by_order as r on r.order_id = running.order_id
)

select
    a.order_line_id,
    a.order_id,
    a.customer_id,
    a.product_id,
    a.order_date,
    a.placed_at,
    a.financial_status,
    a.channel,
    a.quantity,
    a.unit_price_cents,
    a.line_gross_cents,
    a.allocated_refund_cents,
    a.line_gross_cents - a.allocated_refund_cents as line_net_cents,
    a.order_gross_cents,
    a.order_refunded_cents,
    a.last_refund_date,
    c.customer_order_number,
    c.customer_order_number = 1 as is_first_order_for_customer,
    case when c.prior_order_date is null then null else {{ ae_days_between('c.prior_order_date', 'a.order_date') }} end as days_since_prior_order
from allocated as a
inner join customer_orders as c on c.order_id = a.order_id
