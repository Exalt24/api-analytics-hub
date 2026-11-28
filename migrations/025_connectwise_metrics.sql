-- The metric keys the ConnectWise Manage connector writes, per UTC day.
--
--   cw_tickets_opened           service tickets created that day
--   cw_tickets_closed           service tickets closed that day
--   cw_resolution_goals_missed  tickets whose resolution goal fell on that day and were resolved after it, or are still unresolved at the time of the sync (restated by every sync)
--   cw_time_seconds             technician time worked that day, in whole seconds
--
-- None of them is money, so the currency_iff_money check (migration 018) needs no change: a currency on any of them is refused by it already.
--
-- Safe to run twice.

insert into metric (key, label, unit, kind) values
    ('cw_tickets_opened',          'Service tickets opened',          'count',   'delta'),
    ('cw_tickets_closed',          'Service tickets closed',          'count',   'delta'),
    ('cw_resolution_goals_missed', 'Resolution goals missed',         'count',   'delta'),
    ('cw_time_seconds',            'Technician time worked',          'seconds', 'delta')
on conflict (key) do nothing;
