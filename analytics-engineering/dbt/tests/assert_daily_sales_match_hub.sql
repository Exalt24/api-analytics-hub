-- The marts must add back to the hub's own daily figures: for each day, orders, gross sales and distinct customers from fct_orders against the hub's
-- orders, gross_sales and customers. Returns the days that differ (a passing test returns no rows).
with mart as (
    select
        order_date as day,
        count(distinct order_id) as orders,
        sum(line_gross_cents) as gross_cents,
        count(distinct customer_id) as customers
    from {{ ref('fct_orders') }}
    group by order_date
),

hub as (
    select
        observed_on as day,
        max(case when metric_key = 'orders' then value_numeric end) as orders,
        max(case when metric_key = 'gross_sales' then value_numeric end) as gross_cents,
        max(case when metric_key = 'customers' then value_numeric end) as customers
    from {{ ref('stg_hub__daily_figures') }}
    where platform = 'shopify'
    group by observed_on
)

select hub.day, hub.orders as hub_orders, mart.orders as mart_orders, hub.gross_cents as hub_gross, mart.gross_cents as mart_gross,
       hub.customers as hub_customers, mart.customers as mart_customers
from hub
full outer join mart on mart.day = hub.day
where mart.day is null or hub.day is null
   or mart.orders <> hub.orders or mart.gross_cents <> hub.gross_cents or mart.customers <> hub.customers
