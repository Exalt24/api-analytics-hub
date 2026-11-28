-- Money is integer cents on every row. The refund split once produced fractions of a cent on Postgres (division of a numeric sum) while still adding back to the
-- right total, so the total check alone did not see it; this one does. Returns the lines whose money columns are not whole numbers.
select order_line_id, line_gross_cents, allocated_refund_cents, line_net_cents
from {{ ref('fct_orders') }}
where allocated_refund_cents <> cast(allocated_refund_cents as {{ dbt.type_bigint() }})
   or line_gross_cents <> cast(line_gross_cents as {{ dbt.type_bigint() }})
   or line_net_cents <> cast(line_net_cents as {{ dbt.type_bigint() }})
