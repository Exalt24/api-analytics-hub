-- The metric vocabulary. unit says whether a value is a count or money in minor units; kind says whether it is a day's flow (delta) or a level (gauge).
SELECT
    m.key    AS metric_key,
    m.label  AS metric_label,
    m.unit   AS metric_unit,
    m.kind   AS metric_kind
FROM silver_metric m
