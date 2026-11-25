*ProcessBody;
/* Stored process: net sales of one region, streamed back as HTML. */
%global region min_amount;

%stpbegin;
title "Net sales for &region";
proc print data=mart.net_sales noobs;
  where region = "&region" and net_amount >= &min_amount;
  var sale_id region net_amount;
  format region $region.;
run;
%stpend;
