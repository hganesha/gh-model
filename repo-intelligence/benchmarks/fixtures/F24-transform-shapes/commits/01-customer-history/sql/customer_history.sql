create procedure dim.load_customer_history
as
begin
  update dim.customer_history
     set valid_to = current_date, is_current = 0
   where is_current = 1
     and customer_id in (select customer_id from stg.customers_changed);

  insert into dim.customer_history (customer_id, name, region, valid_from, valid_to, is_current)
  select customer_id, name, region, current_date, null, 1
  from stg.customers_changed;
end;
