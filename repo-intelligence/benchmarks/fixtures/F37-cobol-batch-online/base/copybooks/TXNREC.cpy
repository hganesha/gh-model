      *    DAILY TRANSACTION RECORD (SEQUENTIAL, 33 BYTES)
       01  TXN-REC.
           05  TXN-ACCT-ID          PIC 9(10).
           05  TXN-CODE             PIC X(2).
               88  TXN-DEPOSIT      VALUE 'DP'.
               88  TXN-WITHDRAWAL   VALUE 'WD'.
               88  TXN-FEE          VALUE 'FE'.
           05  TXN-AMOUNT           PIC S9(9)V99.
           05  TXN-DATE             PIC X(10).
