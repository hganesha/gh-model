       IDENTIFICATION DIVISION.
       PROGRAM-ID. ACCTINQ.
      *    ONLINE ACCOUNT INQUIRY: TRANSACTION AINQ, MAPSET ACCTMS
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01  WS-MAP-AREA.
           05  MAP-ACCT-ID          PIC X(10).
           05  MAP-ACCT-NAME        PIC X(30).
           05  MAP-BALANCE          PIC -Z(10)9.99.
           05  MAP-MESSAGE          PIC X(40).
       01  WS-COMMAREA.
           05  CA-ACCT-ID           PIC X(10).
       01  WS-ACCT-NAME             PIC X(30).
       01  WS-BALANCE               PIC S9(11)V99 COMP-3.
       01  WS-ACCT-ID               PIC 9(10).
       01  WS-HIST-TRAN             PIC X(4) VALUE 'AHST'.
       01  WS-HIST-COUNT            PIC S9(4) COMP VALUE 0.
           COPY HISTREC.
           EXEC SQL INCLUDE SQLCA END-EXEC.
           EXEC SQL DECLARE HIST-CSR CURSOR FOR
               SELECT TXN_DATE, TXN_CODE, AMOUNT
                 FROM ACCT_HISTORY
                WHERE ACCT_ID = :WS-ACCT-ID
                ORDER BY TXN_DATE DESC
           END-EXEC.
       LINKAGE SECTION.
       01  DFHCOMMAREA              PIC X(10).
       PROCEDURE DIVISION.
       0000-MAIN.
           EXEC CICS HANDLE CONDITION
               MAPFAIL(8000-NO-INPUT)
           END-EXEC
           IF EIBCALEN = 0
               PERFORM 8000-NO-INPUT
           END-IF
           EXEC CICS RECEIVE MAP('ACCTM1') MAPSET('ACCTMS')
               INTO(WS-MAP-AREA)
           END-EXEC
           MOVE MAP-ACCT-ID TO WS-ACCT-ID
           MOVE MAP-ACCT-ID TO CA-ACCT-ID
           PERFORM 1000-LOOKUP
           PERFORM 2000-SHOW
           IF EIBAID = DFHPF5
               PERFORM 3000-HISTORY
           END-IF
           EXEC CICS RETURN TRANSID('AINQ')
               COMMAREA(WS-COMMAREA)
           END-EXEC.
       1000-LOOKUP.
           EXEC SQL
               SELECT ACCT_NAME, BALANCE
                 INTO :WS-ACCT-NAME, :WS-BALANCE
                 FROM ACCOUNTS
                WHERE ACCT_ID = :WS-ACCT-ID
           END-EXEC
           IF SQLCODE NOT = 0
               MOVE 'ACCOUNT NOT FOUND' TO MAP-MESSAGE
           ELSE
               MOVE WS-ACCT-NAME TO MAP-ACCT-NAME
               MOVE WS-BALANCE TO MAP-BALANCE
           END-IF.
       2000-SHOW.
           EXEC CICS SEND MAP('ACCTM1') MAPSET('ACCTMS')
               FROM(WS-MAP-AREA) ERASE
           END-EXEC.
       3000-HISTORY.
           EXEC SQL OPEN HIST-CSR END-EXEC
           PERFORM VARYING WS-HIST-COUNT FROM 1 BY 1
                   UNTIL WS-HIST-COUNT > 10 OR SQLCODE NOT = 0
               EXEC SQL
                   FETCH HIST-CSR
                    INTO :HIST-DATE, :HIST-CODE, :HIST-AMOUNT
               END-EXEC
           END-PERFORM
           EXEC SQL CLOSE HIST-CSR END-EXEC
           EXEC CICS SEND MAP('ACCTM2') MAPSET('ACCTMS')
               FROM(HIST-LINE) ERASE
           END-EXEC
           EXEC CICS START TRANSID(WS-HIST-TRAN)
               FROM(WS-COMMAREA)
           END-EXEC.
       8000-NO-INPUT.
           MOVE 'ENTER AN ACCOUNT NUMBER' TO MAP-MESSAGE
           PERFORM 2000-SHOW
           EXEC CICS RETURN TRANSID('AINQ') END-EXEC.
