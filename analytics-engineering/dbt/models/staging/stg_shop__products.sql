-- grain: one product.
select product_id, product_name, category, list_price_cents, active
from {{ source('shop', 'products') }}
