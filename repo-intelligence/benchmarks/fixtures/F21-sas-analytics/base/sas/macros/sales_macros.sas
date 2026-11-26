/* Shared macros for the sales programs. */

%macro log_step(step=, status=OK);
  data mart.run_log;
    length step $32 status $8;
    step = "&step";
    status = "&status";
    run_ts = datetime();
    format run_ts datetime20.;
  run;
%mend log_step;

%macro month_slice(month);
  /* The month comes from the caller: the dataset name stays a placeholder. */
  data mart.sales_&month;
    set mart.sales_clean;
    where put(sale_date, yymmn6.) = "&month";
  run;
  %log_step(step=month_slice)
%mend month_slice;
