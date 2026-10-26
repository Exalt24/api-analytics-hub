-- The five paid media metric keys the Meta connector writes, and the currency rule for the two that are money.
--
-- 001 hard-coded the money metrics inside the currency_iff_money check, so a new money metric cannot be
-- stored until the check names it: without this, `ad_spend` with a currency would be refused by the
-- database as "a currency on something that is not money", and `ad_spend` without one would be accepted.
-- The check is replaced, not weakened: it still says a currency is required for money and forbidden for
-- everything else, with two more money keys.
--
-- `ad_spend` and `ad_purchase_value` are in the AD ACCOUNT's currency, which need not be the store's. They
-- are never added to a store figure; the report keeps them apart.
--
-- Safe to run twice.

alter table metric_snapshot drop constraint if exists currency_iff_money;
alter table metric_snapshot add constraint currency_iff_money check (
    (currency is not null) = (
        metric_key in ('gross_sales', 'refunds', 'net_sales', 'avg_order_value', 'ad_spend', 'ad_purchase_value')
    )
);

insert into metric (key, label, unit, kind) values
    ('ad_spend',          'Ad spend',       'minor_currency', 'delta'),
    ('ad_impressions',    'Impressions',    'count',          'delta'),
    ('ad_clicks',         'Clicks',         'count',          'delta'),
    ('ad_purchases',      'Purchases',      'count',          'delta'),
    ('ad_purchase_value', 'Purchase value', 'minor_currency', 'delta')
on conflict (key) do nothing;
