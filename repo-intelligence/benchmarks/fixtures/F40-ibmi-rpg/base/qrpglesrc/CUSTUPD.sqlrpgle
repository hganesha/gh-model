**FREE
// Customer credit update: set a customer's limit, log the change and print it.
ctl-opt dftactgrp(*no) actgrp(*caller);

dcl-f CUSTRPT printer oflind(overflow);

dcl-s overflow ind;
dcl-s custNo packed(7:0);
dcl-s newLimit packed(9:2);
dcl-s custName char(30);
dcl-s ftpPassword varchar(40);
dcl-c LOG_ACTION 'LIMIT';

dcl-pr CustLog extpgm('CUSTLOG');
  action char(10) const;
  cust packed(7:0) const;
end-pr;

dcl-pi *n;
  pCust packed(7:0);
  pLimit packed(9:2);
end-pi;

custNo = pCust;
newLimit = pLimit;
ftpPassword = 'fixture-not-a-real-password-0040';

exec sql SELECT CUSNAME INTO :custName
           FROM CUSTMAST
          WHERE CUSNO = :custNo;

if sqlcode = 0;
  exec sql UPDATE CUSTMAST
              SET CUSLIMIT = :newLimit
            WHERE CUSNO = :custNo;
  write RPTLINE;
  CustLog(LOG_ACTION : custNo);
  logChange(custNo : newLimit);
else;
  dsply 'Customer not found';
endif;

*inlr = *on;
return;

dcl-proc logChange;
  dcl-pi *n;
    cust packed(7:0) const;
    amount packed(9:2) const;
  end-pi;

  exec sql INSERT INTO CUSTHIST (CUSNO, CUSLIMIT, CHGDATE)
           VALUES (:cust, :amount, CURRENT DATE);
end-proc;
