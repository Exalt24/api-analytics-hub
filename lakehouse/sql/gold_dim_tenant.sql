-- One row per tenant. The key is the hub's tenant id.
SELECT
    t.id         AS tenant_key,
    t.slug       AS tenant_slug,
    t.name       AS tenant_name
FROM silver_tenant t
