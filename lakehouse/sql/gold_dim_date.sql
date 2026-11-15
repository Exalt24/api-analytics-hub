-- One row per calendar day from the first to the last day any silver figure falls on.
WITH days AS (
    SELECT observed_on AS d FROM silver_daily_figure
    UNION ALL SELECT CAST(created_at AS DATE) FROM silver_lead
    UNION ALL SELECT converted_date FROM silver_lead WHERE is_converted AND converted_date IS NOT NULL
),
bounds AS (SELECT MIN(d) AS lo, MAX(d) AS hi FROM days)
SELECT
    CAST(date_format(d, 'yyyyMMdd') AS INT)  AS date_key,
    d                                        AS date_day,
    year(d)                                  AS year_num,
    month(d)                                 AS month_num,
    date_format(d, 'yyyy-MM')                AS year_month,
    dayofweek(d)                             AS day_of_week,
    date_format(d, 'EEEE')                   AS day_name,
    dayofweek(d) IN (1, 7)                   AS is_weekend
FROM bounds
LATERAL VIEW explode(sequence(lo, hi, interval 1 day)) x AS d
