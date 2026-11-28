-- grain: one ad account, campaign and day, with no gaps: a day a campaign did not deliver is a row of zeros with is_gap_filled true, so the rolling
-- seven day windows (the six days before plus the day itself) always cover seven calendar days once a campaign has run that long.
with campaigns as (
    select distinct account_id, campaign_id, campaign_name, currency from {{ ref('stg_ads__campaign_daily') }}
),

range_of_days as (
    select min(report_date) as first_day, max(report_date) as last_day from {{ ref('stg_ads__campaign_daily') }}
),

grid as (
    select c.account_id, c.campaign_id, c.campaign_name, c.currency, d.date_day
    from campaigns as c
    cross join {{ ref('dim_date') }} as d
    inner join range_of_days as r on d.date_day between r.first_day and r.last_day
),

filled as (
    select
        g.account_id,
        g.campaign_id,
        g.campaign_name,
        g.currency,
        g.date_day,
        a.report_date is null as is_gap_filled,
        coalesce(a.spend_cents, 0) as spend_cents,
        coalesce(a.impressions, 0) as impressions,
        coalesce(a.clicks, 0) as clicks,
        coalesce(a.purchases, 0) as purchases,
        coalesce(a.purchase_value_cents, 0) as purchase_value_cents
    from grid as g
    left join {{ ref('stg_ads__campaign_daily') }} as a
        on a.account_id = g.account_id and a.campaign_id = g.campaign_id and a.report_date = g.date_day
),

windowed as (
    select
        filled.*,
        sum(spend_cents) over w7 as spend_7d_cents,
        sum(purchase_value_cents) over w7 as purchase_value_7d_cents,
        count(*) over w7 as days_in_window,
        sum(spend_cents) over (partition by account_id, date_day) as account_spend_cents
    from filled
    window w7 as (partition by account_id, campaign_id order by date_day rows between 6 preceding and current row)
)

select
    account_id,
    campaign_id,
    campaign_name,
    date_day,
    currency,
    is_gap_filled,
    spend_cents,
    impressions,
    clicks,
    purchases,
    purchase_value_cents,
    spend_7d_cents,
    purchase_value_7d_cents,
    days_in_window,
    {{ ae_safe_divide('purchase_value_cents', 'spend_cents') }} as roas_1d,
    {{ ae_safe_divide('purchase_value_7d_cents', 'spend_7d_cents') }} as roas_7d,
    {{ ae_safe_divide('clicks', 'impressions') }} as click_through_rate,
    {{ ae_safe_divide('spend_cents', 'clicks') }} as cost_per_click_cents,
    {{ ae_safe_divide('spend_cents', 'account_spend_cents') }} as share_of_account_spend,
    rank() over (partition by account_id, date_day order by spend_cents desc, campaign_id) as spend_rank_in_account_day
from windowed
