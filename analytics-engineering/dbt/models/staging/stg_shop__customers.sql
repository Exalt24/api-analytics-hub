-- grain: one customer.
select customer_id, created_at, {{ ae_date_of('created_at') }} as created_date, country, acquisition_channel
from {{ source('shop', 'customers') }}
