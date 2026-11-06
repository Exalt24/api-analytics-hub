-- The metric keys the Google Ads, TikTok, Square, QuickBooks and Shopmonkey connectors write, and the currency rule for the money ones.
--
-- Google Ads and TikTok reuse the five paid media keys 007 created (`ad_spend`, `ad_impressions`, `ad_clicks`) so a dashboard and the combined ad
-- spend view treat every ad platform alike. What 007 did not have:
--   ad_conversions       the conversions the platform counts, in THOUSANDTHS (`milli_count`), because Google's data driven attribution makes
--                        them fractional and a stored integer must not round a day to a whole conversion. Not comparable between platforms
--                        unless the account counts the same actions; the view says which action each platform's figure is.
--   ad_conversion_value  the value of those conversions, in the ad account's currency (money).
--   ad_leads             Local Services Ads leads created that day (a count).
-- The business systems:
--   payments, payments_total, payments_refunded            Square: completed payments, their total money, the refunded part of them.
--   invoices, invoiced_amount, invoice_open_balance        QuickBooks: invoices by invoice date, their total, and their open balance at sync time.
--   work_orders, work_order_revenue                        Shopmonkey: orders created, and the cost of orders invoiced, each on its own date.
--
-- 001 and 007 hard-coded the money metrics inside the currency_iff_money check, so a new money metric cannot be stored until the check names it:
-- without this, `payments_total` with a currency would be refused by the database as "a currency on something that is not money", and without
-- a currency would be accepted. The check is replaced, not weakened: it still says a currency is required for money and forbidden for everything
-- else, with six more money keys.
--
-- Safe to run twice.

-- 001 allowed four units. A conversion count in thousandths is a fifth: it is a count, but one a reader must divide by 1,000.
alter table metric drop constraint if exists metric_unit_check;
alter table metric add constraint metric_unit_check check (unit in ('minor_currency', 'count', 'ratio', 'seconds', 'milli_count'));

alter table metric_snapshot drop constraint if exists currency_iff_money;
alter table metric_snapshot add constraint currency_iff_money check (
    (currency is not null) = (
        metric_key in ('gross_sales', 'refunds', 'net_sales', 'avg_order_value', 'ad_spend', 'ad_purchase_value',
                       'ad_conversion_value', 'payments_total', 'payments_refunded', 'invoiced_amount', 'invoice_open_balance',
                       'work_order_revenue')
    )
);

insert into metric (key, label, unit, kind) values
    ('ad_conversions',       'Conversions',           'milli_count',    'delta'),
    ('ad_conversion_value',  'Conversion value',      'minor_currency', 'delta'),
    ('ad_leads',             'Local Services leads',  'count',          'delta'),
    ('payments',             'Payments',              'count',          'delta'),
    ('payments_total',       'Payments total',        'minor_currency', 'delta'),
    ('payments_refunded',    'Payments refunded',     'minor_currency', 'delta'),
    ('invoices',             'Invoices',              'count',          'delta'),
    ('invoiced_amount',      'Invoiced amount',       'minor_currency', 'delta'),
    ('invoice_open_balance', 'Invoice open balance',  'minor_currency', 'gauge'),
    ('work_orders',          'Work orders',           'count',          'delta'),
    ('work_order_revenue',   'Work order revenue',    'minor_currency', 'delta')
on conflict (key) do nothing;
