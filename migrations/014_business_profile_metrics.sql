-- The metric keys the Google Business Profile connector writes: how a local listing on Google Search and Maps is used, per day.
--
--   gbp_impressions         the four per surface impression counts added (desktop and mobile, Search and Maps). Each surface counts a unique user
--                           once a day, so the sum is how often the listing was shown, not how many people saw it.
--   gbp_calls               taps on the call button
--   gbp_website_clicks      taps on the website link
--   gbp_direction_requests  direction requests to the location
--   gbp_messages            taps on the messaging button
--   gbp_bookings            bookings made from the profile through Reserve with Google
--
-- All six are plain counts, so nothing about the currency check changes: a currency is still required for money and forbidden for everything else.
--
-- Safe to run twice.

insert into metric (key, label, unit, kind) values
    ('gbp_impressions',        'Listing impressions',       'count', 'delta'),
    ('gbp_calls',              'Listing calls',             'count', 'delta'),
    ('gbp_website_clicks',     'Listing website clicks',    'count', 'delta'),
    ('gbp_direction_requests', 'Listing direction requests', 'count', 'delta'),
    ('gbp_messages',           'Listing messages',          'count', 'delta'),
    ('gbp_bookings',           'Listing bookings',          'count', 'delta')
on conflict (key) do nothing;
