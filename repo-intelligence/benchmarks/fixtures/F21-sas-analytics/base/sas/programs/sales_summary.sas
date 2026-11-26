/* Monthly sales summary: clean, summarise, report, export. */
%include "../macros/sales_macros.sas";

%let mart = mart;
%let cutoff = 100;

* Clean the raw extract and classify each sale;
data &mart..sales_clean (drop=i);
  set raw.sales (keep=sale_id region amount sale_date channel);
  array amts{1} amount;
  do i = 1 to dim(amts);
    if missing(amts{i}) then amts{i} = 0;
  end;
  length region_name $12;
  region_name = put(region, $region.);
  if amount < &cutoff then band = 'small';
  else band = 'large';
  format amount amtband.;
run;

/* Returns are netted off through a WORK dataset. */
data returns_by_sale;
  set raw.returns;
  by sale_id;
  retain returned 0;
  if first.sale_id then returned = 0;
  returned + return_amount;
  if last.sale_id then output;
run;

data mart.net_sales;
  merge mart.sales_clean (in=s) returns_by_sale;
  by sale_id;
  if s;
  net_amount = sum(amount, -returned);
run;

proc sort data=mart.net_sales out=sorted_sales;
  by region;
run;

proc sql;
  create table mart.region_summary as
  select region, put(region, $region.) as region_name, count(*) as sales, sum(net_amount) as net
  from sorted_sales
  group by region;
  select count(*) into :nregions trimmed from mart.region_summary;
quit;

%put NOTE: &nregions regions summarised;

ods pdf file="/sasdata/sales/reports/region_summary.pdf" style=journal;
title "Net sales by region";
proc report data=mart.region_summary;
  column region_name sales net;
  define net / analysis sum format=comma12.2;
run;
ods pdf close;

proc export data=mart.region_summary outfile="/sasdata/sales/exports/region_summary.csv" dbms=csv replace;
run;

%month_slice(202601)
%log_step(step=sales_summary)
