//NIGHTLY  PROC
//* A JCL procedure: the .prc suffix is shared with Oracle procedures.
//STEP01   EXEC PGM=ORDLOAD
//INPUT    DD DSN=PROD.ORDERS.DAILY,DISP=SHR
//OUTPUT   DD DSN=PROD.ORDERS.LOADED,DISP=(NEW,CATLG)
//         PEND
