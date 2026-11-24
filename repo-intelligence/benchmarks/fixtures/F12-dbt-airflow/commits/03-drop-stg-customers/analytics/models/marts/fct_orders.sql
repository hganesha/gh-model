{{ config(materialized='incremental', unique_key='order_id') }}

select o.order_id, o.order_date, o.amount, c.region, fx.rate
from {{ ref('stg_orders') }} o
join {{ source('shop', 'customers') }} c on c.id = o.customer_id
left join finance.fx_rates fx on fx.day = o.order_date
{% if is_incremental() %}
where o.order_date > (select max(order_date) from {{ this }})
{% endif %}
