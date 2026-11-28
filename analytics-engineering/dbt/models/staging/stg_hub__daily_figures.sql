-- grain: one source (connection), metric and day, exactly as the hub stores it. Money stays in integer cents with its currency beside it.
select
    s.connection_id,
    c.platform,
    c.display_name as source_name,
    s.metric_key,
    m.unit,
    m.kind,
    s.observed_on,
    s.value_numeric,
    s.currency,
    s.synced_at
from {{ source('hub', 'metric_snapshot') }} as s
inner join {{ source('hub', 'platform_connection') }} as c on c.connection_id = s.connection_id
inner join {{ source('hub', 'metric') }} as m on m.metric_key = s.metric_key
