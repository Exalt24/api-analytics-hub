{#
  The few places Postgres and BigQuery SQL differ, one macro each, so the models read the same on both.
  dbt's own cross database macros (dbt.date_trunc, dbt.dateadd, dbt.datediff, dbt.type_bigint) cover the rest.
  What differs, and what was done about it, is listed in docs/analytics-engineering.md.
#}

{# integer division that truncates on both. `/` is not safe: BigQuery returns a float, and Postgres divides as numeric because sum() of a bigint is numeric (the
   first run of the refund split gave fractions of a cent for that reason). DIV exists in both and returns the whole quotient. #}
{% macro ae_intdiv(a, b) %}cast(div(({{ a }}), ({{ b }})) as {{ dbt.type_bigint() }}){% endmacro %}

{# a ratio that is null, not an error, when the denominator is zero; Postgres needs numeric so the division is not integer division #}
{% macro ae_safe_divide(a, b) %}{{ return(adapter.dispatch('ae_safe_divide', 'analytics_hub')(a, b)) }}{% endmacro %}
{% macro default__ae_safe_divide(a, b) %}(case when ({{ b }}) = 0 then null else cast({{ a }} as numeric) / ({{ b }}) end){% endmacro %}
{% macro bigquery__ae_safe_divide(a, b) %}safe_divide(cast({{ a }} as numeric), ({{ b }})){% endmacro %}

{# ISO weekday, Monday 1 to Sunday 7; BigQuery's DAYOFWEEK runs Sunday 1 to Saturday 7 #}
{% macro ae_iso_weekday(d) %}{{ return(adapter.dispatch('ae_iso_weekday', 'analytics_hub')(d)) }}{% endmacro %}
{% macro default__ae_iso_weekday(d) %}cast(extract(isodow from {{ d }}) as {{ dbt.type_bigint() }}){% endmacro %}
{% macro bigquery__ae_iso_weekday(d) %}mod(extract(dayofweek from {{ d }}) + 5, 7) + 1{% endmacro %}

{# first day of the ISO week (Monday) #}
{% macro ae_week_start(d) %}{{ return(adapter.dispatch('ae_week_start', 'analytics_hub')(d)) }}{% endmacro %}
{% macro default__ae_week_start(d) %}cast(date_trunc('week', {{ d }}) as date){% endmacro %}
{% macro bigquery__ae_week_start(d) %}date_trunc({{ d }}, isoweek){% endmacro %}

{# a column of dates, one per day from start to end inclusive; both bounds are date expressions #}
{% macro ae_date_spine(start_expr, end_expr) %}{{ return(adapter.dispatch('ae_date_spine', 'analytics_hub')(start_expr, end_expr)) }}{% endmacro %}
{% macro default__ae_date_spine(start_expr, end_expr) %}select cast(d as date) as date_day from generate_series(cast({{ start_expr }} as timestamp), cast({{ end_expr }} as timestamp), interval '1 day') as d{% endmacro %}
{% macro bigquery__ae_date_spine(start_expr, end_expr) %}select d as date_day from unnest(generate_date_array({{ start_expr }}, {{ end_expr }}, interval 1 day)) as d{% endmacro %}

{# month name and day name; Postgres to_char pads names with blanks, so trim; BigQuery format_date needs the format first #}
{% macro ae_month_name(d) %}{{ return(adapter.dispatch('ae_month_name', 'analytics_hub')(d)) }}{% endmacro %}
{% macro default__ae_month_name(d) %}trim(to_char({{ d }}, 'Month')){% endmacro %}
{% macro bigquery__ae_month_name(d) %}format_date('%B', {{ d }}){% endmacro %}

{% macro ae_day_name(d) %}{{ return(adapter.dispatch('ae_day_name', 'analytics_hub')(d)) }}{% endmacro %}
{% macro default__ae_day_name(d) %}trim(to_char({{ d }}, 'Day')){% endmacro %}
{% macro bigquery__ae_day_name(d) %}format_date('%A', {{ d }}){% endmacro %}

{# calendar date of a timestamp column; BigQuery casts in UTC, Postgres in the session zone (the loaders write UTC and the container runs UTC) #}
{% macro ae_date_of(ts) %}cast({{ ts }} as date){% endmacro %}

{# whole days from a to b (b later); dbt.datediff(a, b, 'day') already does this on both, wrapped so the call sites say what they mean #}
{% macro ae_days_between(a, b) %}{{ dbt.datediff(a, b, 'day') }}{% endmacro %}
