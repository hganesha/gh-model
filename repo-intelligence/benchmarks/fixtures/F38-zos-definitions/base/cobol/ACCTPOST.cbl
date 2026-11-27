       IDENTIFICATION DIVISION.
       PROGRAM-ID. ACCTPOST.
      * POSTS THE DAILY EXTRACT TO THE ACCOUNT TABLE.
       ENVIRONMENT DIVISION.
       INPUT-OUTPUT SECTION.
       FILE-CONTROL.
           SELECT EXTRACT-IN ASSIGN TO ACCTIN.
       DATA DIVISION.
       FILE SECTION.
       FD  EXTRACT-IN.
       01  EXTRACT-REC.
           05 EX-ACCT-ID          PIC X(10).
           05 EX-AMOUNT           PIC S9(13)V99 COMP-3.
       WORKING-STORAGE SECTION.
           EXEC SQL INCLUDE SQLCA END-EXEC.
           EXEC SQL INCLUDE DACCT END-EXEC.
       01  WS-EOF                 PIC X VALUE 'N'.
       PROCEDURE DIVISION.
       MAIN-PARA.
           OPEN INPUT EXTRACT-IN
           PERFORM UNTIL WS-EOF = 'Y'
               READ EXTRACT-IN
                   AT END MOVE 'Y' TO WS-EOF
                   NOT AT END PERFORM POST-ONE
               END-READ
           END-PERFORM
           CLOSE EXTRACT-IN
           GOBACK.
       POST-ONE.
           MOVE EX-ACCT-ID TO ACCT-ID
           EXEC SQL
               UPDATE ACCT.ACCOUNT
                  SET BALANCE = BALANCE + :EX-AMOUNT
                WHERE ACCT_ID = :ACCT-ID
           END-EXEC.
