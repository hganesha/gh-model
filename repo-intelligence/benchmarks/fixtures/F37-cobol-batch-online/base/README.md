# Account posting (COBOL batch and CICS online)

An account system kept as COBOL source with no build file:

- `cobol/ACCTPOST.cbl`: the nightly batch. Three SELECTs (a sequential input assigned
  to `UT-S-TXNIN`, an indexed master opened I-O, a report opened for output) with FDs
  whose records are copybooks; PERFORM THRU with UNTIL, GO TO an exit paragraph,
  EVALUATE TRUE over 88-levels, READ with AT END and INVALID KEY, REWRITE, STRING and
  WRITE. Two CALLs through data items: `WS-FMT-PGM` has one literal value in the file
  (`'FMTAMT'`), so its CALL resolves; `WS-RULE-PGM` is given `'LOANRULE'` or
  `'DEPRULE'` by an IF, so its CALL stays dynamic. A made-up password
  (`fixture-not-a-real-password-0037`) is MOVEd to `WS-DB-PASSWD`: planted test data
  that must never reach compiler output.
- `cobol/ACCTINQ.cbl`: the CICS inquiry, transaction `AINQ`. HANDLE CONDITION, RECEIVE
  MAP and SEND MAP of map `ACCTM1` in mapset `ACCTMS`, a CICS READ of the file `ACCTFIL`,
  a DB2 SELECT INTO, and RETURN TRANSID.
- `cobol/FMTAMT.cbl`: a subprogram with a LINKAGE SECTION and PROCEDURE DIVISION USING.
- `copybooks/ACCTREC.cpy`, `copybooks/TXNREC.cpy`: record layouts with 88-level
  condition names (single values, a list and THRU ranges) and a COMP-3 balance.

Files the other mainframe packs read, so the COBOL links resolve:

- `cics/maps/ACCTMS.bms`: the BMS mapset `ACCTMS` with map `ACCTM1` (the account number
  as input, name, balance and a message line), which ACCTINQ's SEND MAP RENDERS.
- `.rkp/inputs/zos/csd/ACCTGRP.csd`: a CSD export for group `ACCTGRP`.
  `DEFINE TRANSACTION(AINQ) PROGRAM(ACCTINQ)` makes ACCTINQ expose the transaction it
  also returns to (RETURN TRANSID), and `DEFINE FILE(ACCTFIL) DSNAME(PROD.ACCT.MASTER)`
  makes the CICS file ACCTINQ reads an alias of the master data set.
- `jcl/ACCTNITE.jcl`: the nightly job. Step `POST` runs `EXEC PGM=ACCTPOST` with DDs
  `TXNIN`, `ACCTMST` and `RPTOUT`; joined with ACCTPOST's SELECTs by DD name, ACCTPOST
  reads the day's transactions, reads and writes the master (opened I-O) and writes the
  posting report.

Commits:

1. `01-fee-rule`: the posting paragraph gains a WHEN for fees, which PERFORMs a new
   paragraph with COMPUTE and ON SIZE ERROR.
2. `02-statement-program`: a new free-format program reads accounts through a DB2 cursor
   declared in WORKING-STORAGE and sorts them with input and output procedures.
3. `03-history-screen`: the inquiry gains a history paragraph: a cursor over
   `ACCT_HISTORY`, a second map, and START TRANSID through a data item with one literal
   value; a new copybook holds the history line, and the mapset gains map `ACCTM2`.
