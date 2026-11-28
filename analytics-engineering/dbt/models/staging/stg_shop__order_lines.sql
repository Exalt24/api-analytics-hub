-- grain: one order line. line_gross_cents is quantity times the price paid.
select
    order_line_id,
    order_id,
    product_id,
    quantity,
    unit_price_cents,
    quantity * unit_price_cents as line_gross_cents
from {{ source('shop', 'order_lines') }}
