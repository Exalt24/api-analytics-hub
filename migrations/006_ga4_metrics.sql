-- The four website metrics the GA4 connector writes. Nothing else changes for a second platform:
-- the snapshot table, its unique (connection, metric, day) key, the currency_iff_money check and the
-- forced row level security from 001 already cover these rows. None of them is money, so the check
-- requires a NULL currency, and a row that carries one is refused by the database.
--
-- `users` is a per-day distinct count of people, so like `customers` it is a gauge that cannot be summed
-- across days. The other three are counts of things that happened on the day and can be.
--
-- Safe to run twice.

insert into metric (key, label, unit, kind) values
    ('sessions',   'Sessions',   'count', 'delta'),
    ('users',      'Users',      'count', 'gauge'),
    ('page_views', 'Page views', 'count', 'delta'),
    ('key_events', 'Key events', 'count', 'delta')
on conflict (key) do nothing;
