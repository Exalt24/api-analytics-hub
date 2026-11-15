-- Reporting table: one row per tenant and day, the store's orders and net sales beside the day's ad spend and clicks.
-- Money is in minor units. The ratio is only worked out when the store and the ads are in the same currency and spend is above zero.
SELECT
    tenant_key,
    report_date,
    orders,
    net_sales_minor,
    store_currency,
    ad_spend_minor,
    ad_currency,
    ad_clicks,
    CASE WHEN store_currency = ad_currency AND ad_spend_minor > 0
         THEN ROUND(net_sales_minor / ad_spend_minor, 2) END AS net_sales_per_ad_spend
FROM (
    SELECT
        f.tenant_key                                                              AS tenant_key,
        d.date_day                                                                AS report_date,
        SUM(CASE WHEN f.metric_key = 'orders'    THEN f.figure_value END)         AS orders,
        SUM(CASE WHEN f.metric_key = 'net_sales' THEN f.figure_value END)         AS net_sales_minor,
        MAX(CASE WHEN f.metric_key = 'net_sales' THEN f.currency END)             AS store_currency,
        SUM(CASE WHEN f.metric_key = 'ad_spend'  THEN f.figure_value END)         AS ad_spend_minor,
        MAX(CASE WHEN f.metric_key = 'ad_spend'  THEN f.currency END)             AS ad_currency,
        SUM(CASE WHEN f.metric_key = 'ad_clicks' THEN f.figure_value END)         AS ad_clicks
    FROM gold_fact_daily_figure f
    JOIN gold_dim_date d ON d.date_key = f.date_key
    WHERE f.metric_key IN ('orders', 'net_sales', 'ad_spend', 'ad_clicks')
    GROUP BY f.tenant_key, d.date_day
) r
