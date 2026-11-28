-- The metric keys the Autotask connector writes, per UTC day.
--
--   at_tickets_opened           tickets created that day
--   at_tickets_closed           tickets completed that day (the day of completedDate)
--   at_resolution_goals_missed  tickets whose resolution goal fell on that day and were resolved after it, or are still unresolved at the time of the sync (restated by every sync)
--   at_time_seconds             technician time worked that day, in whole seconds
--
-- None of them is money, so the currency_iff_money check (migration 018) needs no change: a currency on any of them is refused by it already.
--
-- Safe to run twice.

insert into metric (key, label, unit, kind) values
    ('at_tickets_opened',          'Service tickets opened',          'count',   'delta'),
    ('at_tickets_closed',          'Service tickets completed',       'count',   'delta'),
    ('at_resolution_goals_missed', 'Resolution goals missed',         'count',   'delta'),
    ('at_time_seconds',            'Technician time worked',          'seconds', 'delta')
on conflict (key) do nothing;
