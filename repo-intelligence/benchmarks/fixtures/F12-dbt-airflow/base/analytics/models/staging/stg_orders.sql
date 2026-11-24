select
    id as order_id,
    customer_id,
    amount_cents / 100.0 as amount,
    created_at::date as order_date
from {{ source('shop', 'orders') }}
where status != 'test'
