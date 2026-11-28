{% test unique_combination(model, columns) %}
{#- fails for every combination of the columns that appears more than once; a grain test for tables with no single key column -#}
select {{ columns | join(', ') }}, count(*) as row_count
from {{ model }}
group by {{ columns | join(', ') }}
having count(*) > 1
{% endtest %}
