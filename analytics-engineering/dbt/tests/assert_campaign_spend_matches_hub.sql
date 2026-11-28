-- The campaigns of the ad account must add back to the hub's ad account figures each day (spend, clicks, purchase value). Returns the days that differ.
with mart as (
    select date_day as day, sum(spend_cents) as spend_cents, sum(clicks) as clicks, sum(purchase_value_cents) as purchase_value_cents
    from {{ ref('fct_ad_spend_daily') }}
    group by date_day
),

hub as (
    select
        observed_on as day,
        max(case when metric_key = 'ad_spend' then value_numeric end) as spend_cents,
        max(case when metric_key = 'ad_clicks' then value_numeric end) as clicks,
        max(case when metric_key = 'ad_purchase_value' then value_numeric end) as purchase_value_cents
    from {{ ref('stg_hub__daily_figures') }}
    where platform = 'meta'
    group by observed_on
)

select hub.day, hub.spend_cents as hub_spend, mart.spend_cents as mart_spend, hub.clicks as hub_clicks, mart.clicks as mart_clicks
from hub
full outer join mart on mart.day = hub.day
where mart.day is null or hub.day is null
   or mart.spend_cents <> hub.spend_cents or mart.clicks <> hub.clicks or mart.purchase_value_cents <> hub.purchase_value_cents
