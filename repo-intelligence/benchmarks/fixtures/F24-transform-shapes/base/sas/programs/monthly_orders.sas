/* Monthly order analysis. */
libname raw "/data/raw";
libname mart "/data/mart";

proc sort data=raw.orders out=work.orders_sorted;
  by customer_id descending order_date;
run;

/* The latest order of each customer. */
data mart.last_order;
  set work.orders_sorted;
  by customer_id descending order_date;
  if first.customer_id;
run;

/* Customers who placed no order. */
data mart.no_orders;
  merge raw.customers(in=c) work.orders_sorted(in=o keep=customer_id);
  by customer_id;
  if c and not o;
run;

/* Total spend per customer. */
data mart.customer_spend(keep=customer_id total_spend);
  set work.orders_sorted;
  by customer_id;
  retain total_spend;
  if first.customer_id then total_spend = 0;
  total_spend + amount;
  if last.customer_id then output;
run;

proc transpose data=mart.monthly_long out=mart.monthly_wide prefix=m_;
  by customer_id;
  id month;
  var amount;
run;

proc means data=raw.orders nway noprint;
  class region;
  var amount;
  output out=mart.region_summary sum=total_amount mean=avg_amount;
run;
