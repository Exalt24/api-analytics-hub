-- grain: one refund, booked against one order on its own day (which is often a later day than the order's).
select
    refund_id,
    order_id,
    refunded_at,
    {{ ae_date_of('refunded_at') }} as refund_date,
    amount_cents,
    reason
from {{ source('shop', 'refunds') }}
