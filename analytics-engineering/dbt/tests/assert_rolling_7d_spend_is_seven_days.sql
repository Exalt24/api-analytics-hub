-- The rolling seven day spend must equal the plain sum of the same campaign's spend over the seven calendar days ending that day, worked out here with a
-- self join instead of a window. Only days with a full window are compared. Returns the rows that differ.
select f.account_id, f.campaign_id, f.date_day, f.spend_7d_cents, sum(p.spend_cents) as check_cents
from {{ ref('fct_ad_spend_daily') }} as f
inner join {{ ref('fct_ad_spend_daily') }} as p
    on p.account_id = f.account_id
   and p.campaign_id = f.campaign_id
   and p.date_day between {{ dbt.dateadd('day', -6, 'f.date_day') }} and f.date_day
where f.days_in_window = 7
group by f.account_id, f.campaign_id, f.date_day, f.spend_7d_cents
having sum(p.spend_cents) <> f.spend_7d_cents
