-- grain: one order. placed_at is a UTC timestamp, order_date its calendar day.
select
    order_id,
    customer_id,
    placed_at,
    {{ ae_date_of('placed_at') }} as order_date,
    currency,
    financial_status,
    channel
from {{ source('shop', 'orders') }}
