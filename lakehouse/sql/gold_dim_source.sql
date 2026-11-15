-- One row per connected source account (a Shopify store, an ad account, a Salesforce org). The key is the hub's connection id.
-- Credentials never reach the lakehouse: the bronze read selects no credential column.
SELECT
    c.id                                       AS source_key,
    c.tenant_id                                AS tenant_key,
    c.platform                                 AS platform,
    c.external_account                         AS external_account,
    COALESCE(c.display_name, c.external_account) AS source_name,
    c.status                                   AS source_status
FROM silver_platform_connection c
