-- grain: one calendar day, from the first to the last day the hub has a figure for, with no gaps. Built from a date spine (the one place
-- Postgres and BigQuery need different SQL for it, see macros/dialect.sql), so a day with no orders still has a row to join to.
with bounds as (
    select min(observed_on) as first_day, max(observed_on) as last_day
    from {{ ref('stg_hub__daily_figures') }}
),

spine as (
    {{ ae_date_spine('(select first_day from bounds)', '(select last_day from bounds)') }}
)

select
    date_day,
    cast(extract(year from date_day) as {{ dbt.type_bigint() }}) as year_number,
    cast(extract(quarter from date_day) as {{ dbt.type_bigint() }}) as quarter_number,
    cast(extract(month from date_day) as {{ dbt.type_bigint() }}) as month_number,
    {{ ae_month_name('date_day') }} as month_name,
    cast({{ dbt.date_trunc('month', 'date_day') }} as date) as month_start,
    {{ ae_week_start('date_day') }} as week_start,
    {{ ae_iso_weekday('date_day') }} as iso_weekday,
    {{ ae_day_name('date_day') }} as day_name,
    {{ ae_iso_weekday('date_day') }} >= 6 as is_weekend,
    cast(extract(day from date_day) as {{ dbt.type_bigint() }}) as day_of_month,
    date_day = cast({{ dbt.dateadd('day', -1, dbt.dateadd('month', 1, dbt.date_trunc('month', 'date_day'))) }} as date) as is_month_end
from spine
