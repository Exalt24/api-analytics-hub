-- The fact table. Grain: one row per source account, day and metric. Money stays in minor units with its currency beside it.
-- Salesforce leads arrive as records, so the daily counts are made here from silver_lead and joined to their source account
-- through the org id; a lead whose org has no connection drops out of the fact and the reconciliation check names it.
SELECT tenant_key, source_key, date_key, metric_key, figure_value, currency
FROM (
    SELECT
        s.tenant_id                                          AS tenant_key,
        s.connection_id                                      AS source_key,
        CAST(date_format(s.observed_on, 'yyyyMMdd') AS INT)  AS date_key,
        s.metric_key                                         AS metric_key,
        s.value_numeric                                      AS figure_value,
        s.currency                                           AS currency
    FROM silver_daily_figure s

    UNION ALL

    SELECT
        c.tenant_id,
        c.id,
        CAST(date_format(CAST(l.created_at AS DATE), 'yyyyMMdd') AS INT),
        'sf_leads_created',
        COUNT(*),
        CAST(NULL AS STRING)
    FROM silver_lead l
    JOIN silver_platform_connection c ON c.platform = 'salesforce' AND c.external_account = l.org_id
    GROUP BY c.tenant_id, c.id, CAST(l.created_at AS DATE)

    UNION ALL

    SELECT
        c.tenant_id,
        c.id,
        CAST(date_format(l.converted_date, 'yyyyMMdd') AS INT),
        'sf_leads_converted',
        COUNT(*),
        CAST(NULL AS STRING)
    FROM silver_lead l
    JOIN silver_platform_connection c ON c.platform = 'salesforce' AND c.external_account = l.org_id
    WHERE l.is_converted AND l.converted_date IS NOT NULL
    GROUP BY c.tenant_id, c.id, l.converted_date
) f
