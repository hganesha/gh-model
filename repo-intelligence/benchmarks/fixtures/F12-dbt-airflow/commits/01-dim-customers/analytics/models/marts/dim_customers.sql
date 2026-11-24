select c.customer_id, c.region, count(o.order_id) as orders
from {{ ref('stg_customers') }} c
left join {{ ref('stg_orders') }} o using (customer_id)
group by 1, 2
