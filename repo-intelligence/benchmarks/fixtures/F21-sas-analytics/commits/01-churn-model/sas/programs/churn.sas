/* Churn: fit on customer history, keep the coefficients, score the current month. */
proc logistic data=mart.customer_history outest=mart.churn_coef noprint;
  class region / param=ref;
  model churned(event='1') = tenure net_amount region;
run;

proc score data=mart.customers score=mart.churn_coef out=mart.churn_scores type=parms;
  var tenure net_amount;
run;
