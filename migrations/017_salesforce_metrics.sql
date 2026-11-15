-- The metric keys the Salesforce connector writes: leads and opportunities per day.
--
--   sf_leads_created          Lead rows created that day (the UTC day of CreatedDate)
--   sf_leads_converted        Lead rows converted that day (ConvertedDate, a date with no time zone)
--   sf_opportunities_created  Opportunity rows created that day (the UTC day of CreatedDate)
--   sf_opportunities_won      Opportunity rows won that day (IsWon, by CloseDate)
--   sf_won_amount             the Amount of those won opportunities, in the connection's currency (money)
--
-- The first four are plain counts, so nothing changes for them: a currency is still forbidden. `sf_won_amount` is money, and 001 and 013 hard-coded the money
-- metrics inside the currency_iff_money check, so the check is replaced here with the same list plus the one new key. It still says a currency is required
-- for money and forbidden for everything else.
--
-- Safe to run twice.

alter table metric_snapshot drop constraint if exists currency_iff_money;
alter table metric_snapshot add constraint currency_iff_money check (
    (currency is not null) = (
        metric_key in ('gross_sales', 'refunds', 'net_sales', 'avg_order_value', 'ad_spend', 'ad_purchase_value',
                       'ad_conversion_value', 'payments_total', 'payments_refunded', 'invoiced_amount', 'invoice_open_balance',
                       'work_order_revenue', 'sf_won_amount')
    )
);

insert into metric (key, label, unit, kind) values
    ('sf_leads_created',         'Leads created',         'count',          'delta'),
    ('sf_leads_converted',       'Leads converted',       'count',          'delta'),
    ('sf_opportunities_created', 'Opportunities created', 'count',          'delta'),
    ('sf_opportunities_won',     'Opportunities won',     'count',          'delta'),
    ('sf_won_amount',            'Won amount',            'minor_currency', 'delta')
on conflict (key) do nothing;
