-- Staging load: run nightly after the raw extract lands.

insert into stg.orders_latest
select *
from (
    select o.*,
           row_number() over (partition by order_id order by updated_at desc) as rn
    from raw.orders o
) ranked
where rn = 1;

merge into stg.customers t
using raw.customers s
on t.customer_id = s.customer_id
when matched then update set name = s.name, region = s.region
when not matched then insert (customer_id, name, region) values (s.customer_id, s.name, s.region);

insert into mart.region_daily
select region, order_date, sum(amount) as total_amount, count(*) as order_count
from stg.orders_latest
group by region, order_date;

insert into stg.orders_clean
select order_id,
       trim(customer_name) as customer_name,
       upper(status) as status,
       coalesce(discount, 0) as discount,
       case status when 'O' then 'Open' when 'C' then 'Closed' when 'X' then 'Cancelled' end as status_name,
       case when amount > 1000 then 1 else 0 end as is_large
from stg.orders_latest;
