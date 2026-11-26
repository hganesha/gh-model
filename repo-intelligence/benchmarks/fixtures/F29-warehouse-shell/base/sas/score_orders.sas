/* Score the day's orders. */
libname stg '/data/sas/stg';
libname mart '/data/sas/mart';

data mart.order_scores;
  set stg.orders;
  score = amount / 100;
run;
