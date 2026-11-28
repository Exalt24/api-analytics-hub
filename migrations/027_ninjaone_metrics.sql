-- The metric keys the NinjaOne connector writes.
--
--   nj_devices          devices NinjaOne lists, as they were when the sync ran (a gauge, written on the last day of the sync's window)
--   nj_devices_offline  the same, for devices that were offline (a gauge)
--   nj_alerts_active    active alerts (triggered conditions) when the sync ran (a gauge)
--   nj_alerts_created   active alerts whose creation time falls on that day (a count; an alert reset before the sync is not listed, so it is not counted)
--
-- The three gauges are point in time figures, which is why the query layer must never sum them across days. None of the four is money, so the currency_iff_money check
-- (migration 018) needs no change: a currency on any of them is refused by it already.
--
-- Safe to run twice.

insert into metric (key, label, unit, kind) values
    ('nj_devices',         'Devices managed',         'count', 'gauge'),
    ('nj_devices_offline', 'Devices offline',         'count', 'gauge'),
    ('nj_alerts_active',   'Alerts active',           'count', 'gauge'),
    ('nj_alerts_created',  'Active alerts raised',    'count', 'delta')
on conflict (key) do nothing;
