CREATE SCHEMA IF NOT EXISTS workspace.medallion_demo;
CREATE OR REPLACE TABLE workspace.medallion_demo.bronze_orders AS
WITH base AS (
  SELECT id AS order_id,
         concat('C', lpad(cast(id % 200 AS string), 3, '0')) AS customer_id,
         CASE WHEN id % 97 = 0 THEN NULL ELSE round((id % 500) * 1.37 + 5, 2) END AS amount,
         CASE WHEN id % 53 = 0 THEN 'not-a-date' ELSE date_format(date_add(DATE'2026-01-01', cast(id % 120 AS int)), 'yyyy-MM-dd') END AS order_date
  FROM range(1, 5001) AS t(id)
)
SELECT b.*, 'generated_batch_1' AS source_file, current_timestamp() AS ingested_at,
       sha2(concat_ws('|', cast(order_id AS string), customer_id, cast(amount AS string), order_date), 256) AS row_hash
FROM base b
UNION ALL
SELECT b.*, 'generated_batch_2' AS source_file, current_timestamp() AS ingested_at,
       sha2(concat_ws('|', cast(order_id AS string), customer_id, cast(amount AS string), order_date), 256) AS row_hash
FROM base b WHERE order_id % 40 = 0;
CREATE OR REPLACE TABLE workspace.medallion_demo.quarantine_orders AS
SELECT *, CASE WHEN amount IS NULL THEN 'missing amount' ELSE 'bad order date' END AS reason
FROM workspace.medallion_demo.bronze_orders
WHERE amount IS NULL OR try_to_date(order_date, 'yyyy-MM-dd') IS NULL;
CREATE OR REPLACE TABLE workspace.medallion_demo.silver_orders AS
SELECT order_id, customer_id, amount, to_date(order_date) AS order_date, source_file, ingested_at
FROM (
  SELECT *, row_number() OVER (PARTITION BY order_id ORDER BY ingested_at, source_file) AS rn
  FROM workspace.medallion_demo.bronze_orders
  WHERE amount IS NOT NULL AND try_to_date(order_date, 'yyyy-MM-dd') IS NOT NULL
) WHERE rn = 1;
CREATE OR REPLACE TABLE workspace.medallion_demo.gold_daily_revenue AS
SELECT order_date, count(*) AS orders, round(sum(amount), 2) AS revenue, count(DISTINCT customer_id) AS customers
FROM workspace.medallion_demo.silver_orders GROUP BY order_date;
SELECT 'bronze rows' AS item, cast(count(*) AS string) AS value FROM workspace.medallion_demo.bronze_orders
UNION ALL SELECT 'quarantined rows (missing amount or bad date)', cast(count(*) AS string) FROM workspace.medallion_demo.quarantine_orders
UNION ALL SELECT 'silver rows (valid, one per order id)', cast(count(*) AS string) FROM workspace.medallion_demo.silver_orders
UNION ALL SELECT 'valid duplicate rows removed', cast((SELECT count(*) FROM workspace.medallion_demo.bronze_orders WHERE amount IS NOT NULL AND try_to_date(order_date, 'yyyy-MM-dd') IS NOT NULL) - (SELECT count(*) FROM workspace.medallion_demo.silver_orders) AS string)
UNION ALL SELECT 'CHECK rows add up: bronze = silver + quarantine + duplicates removed', CASE WHEN (SELECT count(*) FROM workspace.medallion_demo.bronze_orders) = (SELECT count(*) FROM workspace.medallion_demo.silver_orders) + (SELECT count(*) FROM workspace.medallion_demo.quarantine_orders) + ((SELECT count(*) FROM workspace.medallion_demo.bronze_orders WHERE amount IS NOT NULL AND try_to_date(order_date, 'yyyy-MM-dd') IS NOT NULL) - (SELECT count(*) FROM workspace.medallion_demo.silver_orders)) THEN 'PASS' ELSE 'FAIL' END
UNION ALL SELECT 'CHECK no duplicate order id in silver', CASE WHEN (SELECT count(*) FROM workspace.medallion_demo.silver_orders) = (SELECT count(DISTINCT order_id) FROM workspace.medallion_demo.silver_orders) THEN 'PASS' ELSE 'FAIL' END
UNION ALL SELECT 'CHECK no null amount in silver', CASE WHEN (SELECT count(*) FROM workspace.medallion_demo.silver_orders WHERE amount IS NULL) = 0 THEN 'PASS' ELSE 'FAIL' END
UNION ALL SELECT 'CHECK gold revenue equals silver amount', CASE WHEN abs((SELECT sum(revenue) FROM workspace.medallion_demo.gold_daily_revenue) - (SELECT sum(amount) FROM workspace.medallion_demo.silver_orders)) < 0.01 THEN 'PASS' ELSE 'FAIL' END
UNION ALL SELECT 'gold days', cast(count(*) AS string) FROM workspace.medallion_demo.gold_daily_revenue;
