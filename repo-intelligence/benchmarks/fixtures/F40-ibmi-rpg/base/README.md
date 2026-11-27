# Customer desk (IBM i)

An IBM i customer desk kept as source members, one directory per source file:

- `qddssrc/CUSTMAST.pf`: the customer master physical file: a record format, four fields
  (one with a VALUES list) and a key.
- `qddssrc/CUSTL1.lf`: a logical file over CUSTMAST, keyed by name, selecting active
  customers.
- `qddssrc/CUSTDSP.dspf`: a display file with two record formats: an input field, fields
  that refer to CUSTMAST, a VALUES list, command keys and constants.
- `qddssrc/CUSTRPT.prtf`: a printer file with a heading and a detail line.
- `qrpglesrc/CUSTINQ.rpgle`: a fixed-form RPG IV inquiry: F-specs for CUSTMAST and the
  display file, an *ENTRY PLIST, a DOW loop, EXFMT, CHAIN with a resulting indicator and
  a subroutine.
- `qrpglesrc/CUSTUPD.sqlrpgle`: a fully free SQLRPGLE update: a printer file, embedded
  SELECT INTO and UPDATE, a call of another program through an EXTPGM prototype and a
  procedure that inserts into a history table. The transfer password it sets is planted test
  data that the secret scanner must find and that must never reach the graph.
- `qclsrc/CUSTJOB.clle`: the CL program that runs them: it calls the inquiry, submits the
  update to batch or calls it, overrides CUSTMAST, reads and changes a data area, monitors
  messages and calls a program whose name it reads from the data area at run time. The
  user it submits the batch job as and the value it puts in the data area are planted
  test data that must never reach the graph.
