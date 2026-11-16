-- The metric keys the Harvest and Microsoft Graph connectors write.
--
-- Harvest (time entries, per day):
--   harvest_hours              hours tracked that day, in whole seconds
--   harvest_billable_hours     the same for billable entries, in whole seconds
--   harvest_time_entries       entries logged that day
--   harvest_billable_amount    rounded hours x billable rate of the billable entries, in the connection's currency (money)
--
-- Microsoft Graph (a SharePoint list register and one Teams channel, per day):
--   sp_register_items_created   list rows created that day (the UTC day)
--   sp_register_items_modified  list rows last modified that day (the UTC day)
--   teams_channel_messages      top level channel messages posted that day (the UTC day)
--
-- Only `harvest_billable_amount` is money, and 001, 013 and 017 hard-coded the money metrics inside the currency_iff_money check, so the check is replaced here with the
-- same list plus the one new key. It still says a currency is required for money and forbidden for everything else.
--
-- Safe to run twice.

alter table metric_snapshot drop constraint if exists currency_iff_money;
alter table metric_snapshot add constraint currency_iff_money check (
    (currency is not null) = (
        metric_key in ('gross_sales', 'refunds', 'net_sales', 'avg_order_value', 'ad_spend', 'ad_purchase_value',
                       'ad_conversion_value', 'payments_total', 'payments_refunded', 'invoiced_amount', 'invoice_open_balance',
                       'work_order_revenue', 'sf_won_amount', 'harvest_billable_amount')
    )
);

insert into metric (key, label, unit, kind) values
    ('harvest_hours',              'Hours tracked',            'seconds',        'delta'),
    ('harvest_billable_hours',     'Billable hours tracked',   'seconds',        'delta'),
    ('harvest_time_entries',       'Time entries logged',      'count',          'delta'),
    ('harvest_billable_amount',    'Billable amount',          'minor_currency', 'delta'),
    ('sp_register_items_created',  'Register rows created',    'count',          'delta'),
    ('sp_register_items_modified', 'Register rows modified',   'count',          'delta'),
    ('teams_channel_messages',     'Teams channel messages',   'count',          'delta')
on conflict (key) do nothing;
