       IDENTIFICATION DIVISION.
       PROGRAM-ID. ACCTPOST.
      *    NIGHTLY POSTING OF THE DAY'S TRANSACTIONS TO THE MASTER
       ENVIRONMENT DIVISION.
       INPUT-OUTPUT SECTION.
       FILE-CONTROL.
           SELECT TXN-FILE ASSIGN TO UT-S-TXNIN
               FILE STATUS IS WS-TXN-STATUS.
           SELECT ACCT-MASTER ASSIGN TO ACCTMST
               ORGANIZATION IS INDEXED
               ACCESS MODE IS RANDOM
               RECORD KEY IS ACCT-ID
               FILE STATUS IS WS-ACCT-STATUS.
           SELECT POST-REPORT ASSIGN TO RPTOUT.
       DATA DIVISION.
       FILE SECTION.
       FD  TXN-FILE
           RECORDING MODE IS F
           RECORD CONTAINS 33 CHARACTERS.
           COPY TXNREC.
       FD  ACCT-MASTER.
           COPY ACCTREC.
       FD  POST-REPORT.
       01  REPORT-LINE              PIC X(132).
       WORKING-STORAGE SECTION.
       01  WS-TXN-STATUS            PIC XX.
           88  TXN-EOF              VALUE '10'.
       01  WS-ACCT-STATUS           PIC XX.
       01  WS-COUNTS.
           05  WS-READ-COUNT        PIC 9(7) COMP VALUE 0.
           05  WS-POSTED-COUNT      PIC 9(7) COMP VALUE 0.
           05  WS-REJECT-COUNT      PIC 9(7) COMP VALUE 0.
       01  WS-FMT-PGM               PIC X(8) VALUE 'FMTAMT'.
       01  WS-RULE-PGM              PIC X(8).
       01  WS-DB-PASSWD             PIC X(32).
       01  WS-AMOUNT-TEXT           PIC X(16).
       PROCEDURE DIVISION.
       0000-MAIN.
           PERFORM 1000-INIT
           PERFORM 2000-READ-TXN THRU 2000-EXIT
               UNTIL TXN-EOF
           PERFORM 9000-FINISH
           STOP RUN.
       1000-INIT.
           OPEN INPUT TXN-FILE
           OPEN I-O ACCT-MASTER
           OPEN OUTPUT POST-REPORT
           MOVE 'fixture-not-a-real-password-0037' TO WS-DB-PASSWD
           INITIALIZE WS-COUNTS.
       2000-READ-TXN.
           READ TXN-FILE
               AT END GO TO 2000-EXIT
           END-READ
           ADD 1 TO WS-READ-COUNT
           MOVE TXN-ACCT-ID TO ACCT-ID
           READ ACCT-MASTER
               INVALID KEY
                   ADD 1 TO WS-REJECT-COUNT
                   GO TO 2000-EXIT
           END-READ
           PERFORM 3000-POST.
       2000-EXIT.
           EXIT.
       3000-POST.
           EVALUATE TRUE
               WHEN TXN-DEPOSIT
                   ADD TXN-AMOUNT TO ACCT-BALANCE
               WHEN TXN-WITHDRAWAL
                   SUBTRACT TXN-AMOUNT FROM ACCT-BALANCE
               WHEN OTHER
                   ADD 1 TO WS-REJECT-COUNT
           END-EVALUATE
           IF ACCT-LOAN
               MOVE 'LOANRULE' TO WS-RULE-PGM
           ELSE
               MOVE 'DEPRULE' TO WS-RULE-PGM
           END-IF
           CALL WS-RULE-PGM USING ACCT-REC TXN-REC
           MOVE TXN-DATE TO ACCT-LAST-TXN-DATE
           REWRITE ACCT-REC
               INVALID KEY ADD 1 TO WS-REJECT-COUNT
               NOT INVALID KEY ADD 1 TO WS-POSTED-COUNT
           END-REWRITE
           CALL WS-FMT-PGM USING TXN-AMOUNT WS-AMOUNT-TEXT
           STRING TXN-ACCT-ID ' ' WS-AMOUNT-TEXT
               DELIMITED BY SIZE INTO REPORT-LINE
           WRITE REPORT-LINE.
       9000-FINISH.
           CLOSE TXN-FILE ACCT-MASTER POST-REPORT.
