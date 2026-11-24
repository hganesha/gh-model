select id as customer_id, email, region
from {{ source('shop', 'customers') }}
