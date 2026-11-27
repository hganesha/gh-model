      *    ONE LINE OF ACCOUNT HISTORY, AS THE HISTORY MAP SHOWS IT
       01  HIST-LINE.
           05  HIST-DATE            PIC X(10).
           05  HIST-CODE            PIC X(2).
               88  HIST-DEPOSIT     VALUE 'DP'.
               88  HIST-WITHDRAWAL  VALUE 'WD'.
               88  HIST-FEE         VALUE 'FE'.
           05  HIST-AMOUNT          PIC S9(9)V99 COMP-3.
