-- Each customer has exactly one first order: the lines flagged as a first order all belong to one order. Returns customers with none or with more than one.
select customer_id, count(distinct case when is_first_order_for_customer then order_id end) as first_orders
from {{ ref('fct_orders') }}
group by customer_id
having count(distinct case when is_first_order_for_customer then order_id end) <> 1
