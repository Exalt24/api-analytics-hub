-- Reporting table: for each source account, the last day it has a figure for, how far that is behind the as-of date, and whether that is inside the limit.
-- The run fills in the as-of date and the stated limit in days where the two named markers stand.
SELECT
    tenant_key,
    source_key,
    platform,
    source_name,
    last_observed_on,
    CAST(:as_of AS DATE)                                  AS as_of_date,
    datediff(CAST(:as_of AS DATE), last_observed_on)      AS lag_days,
    CAST(:limit_days AS INT)                              AS limit_days,
    CASE
        WHEN last_observed_on IS NULL THEN 'NO_DATA'
        WHEN datediff(CAST(:as_of AS DATE), last_observed_on) <= CAST(:limit_days AS INT) THEN 'FRESH'
        ELSE 'STALE'
    END                                                   AS freshness_status
FROM (
    SELECT
        s.tenant_key       AS tenant_key,
        s.source_key       AS source_key,
        s.platform         AS platform,
        s.source_name      AS source_name,
        MAX(d.date_day)    AS last_observed_on
    FROM gold_dim_source s
    LEFT JOIN gold_fact_daily_figure f ON f.source_key = s.source_key
    LEFT JOIN gold_dim_date d ON d.date_key = f.date_key
    GROUP BY s.tenant_key, s.source_key, s.platform, s.source_name
) x
