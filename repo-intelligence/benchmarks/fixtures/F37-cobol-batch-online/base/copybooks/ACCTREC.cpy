      *    ACCOUNT MASTER RECORD (VSAM KSDS, KEY ACCT-ID)
       01  ACCT-REC.
           05  ACCT-ID              PIC 9(10).
           05  ACCT-NAME            PIC X(30).
           05  ACCT-TYPE            PIC X(2).
               88  ACCT-CHECKING    VALUE 'CK'.
               88  ACCT-SAVINGS     VALUE 'SV'.
               88  ACCT-LOAN        VALUE 'LN' 'LC'.
           05  ACCT-STATUS          PIC X.
               88  ACCT-ACTIVE      VALUE 'A'.
               88  ACCT-FROZEN      VALUE 'F'.
               88  ACCT-CLOSED      VALUE 'C'.
           05  ACCT-BALANCE         PIC S9(11)V99 COMP-3.
           05  ACCT-RISK-SCORE      PIC 9(3).
               88  ACCT-LOW-RISK    VALUE 0 THRU 299.
               88  ACCT-HIGH-RISK   VALUE 700 THRU 999.
           05  ACCT-LAST-TXN-DATE   PIC X(10).
           05  FILLER               PIC X(20).
