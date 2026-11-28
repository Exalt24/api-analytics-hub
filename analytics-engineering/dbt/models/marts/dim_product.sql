-- grain: one product. Sales figures come from fct_orders; the rank is a window over the product's category.
with sold as (
    select
        product_id,
        sum(quantity) as units_sold,
        sum(line_gross_cents) as gross_cents,
        sum(line_net_cents) as net_cents,
        count(distinct order_id) as orders_containing,
        min(order_date) as first_sold_on,
        max(order_date) as last_sold_on
    from {{ ref('fct_orders') }}
    group by product_id
)

select
    p.product_id,
    p.product_name,
    p.category,
    p.list_price_cents,
    p.active,
    case when p.list_price_cents < 2500 then 'low' when p.list_price_cents < 5000 then 'mid' else 'high' end as price_band,
    coalesce(s.units_sold, 0) as units_sold,
    coalesce(s.gross_cents, 0) as gross_cents,
    coalesce(s.net_cents, 0) as net_cents,
    coalesce(s.orders_containing, 0) as orders_containing,
    s.first_sold_on,
    s.last_sold_on,
    rank() over (partition by p.category order by coalesce(s.net_cents, 0) desc) as net_revenue_rank_in_category
from {{ ref('stg_shop__products') }} as p
left join sold as s on s.product_id = p.product_id
