/* Monthly customer load into the reporting library */
libname stage '/data/stage';
libname rpt oracle path=rptdb schema=reporting;
%include '/sas/macros/audit.sas';

%let month = %sysfunc(today(), yymmn6.);

data stage.customers_&month;
  set rpt.customer_extract;
  where status = 'A';
run;

proc sql;
  create table rpt.customer_summary as
  select region, count(*) as customers
  from stage.customers_&month
  group by region;
quit;

proc sort data=rpt.customer_summary out=work.sorted;
  by region;
run;

%audit_log(table=rpt.customer_summary);
