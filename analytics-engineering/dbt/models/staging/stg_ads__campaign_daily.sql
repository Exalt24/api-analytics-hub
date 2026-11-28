-- grain: one ad account, campaign and day (only days the campaign delivered appear; the mart fills the gaps).
select
    account_id,
    campaign_id,
    campaign_name,
    report_date,
    spend_cents,
    impressions,
    clicks,
    purchases,
    purchase_value_cents,
    currency
from {{ source('ads', 'ads_campaign_daily') }}
