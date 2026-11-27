/* REXX - RUNRPT: refresh the customer file and print the balance report */
ADDRESS TSO
parse arg region .
password = 'fixture-not-a-real-password-0041'

/* Customer master in, refreshed master out. */
"ALLOC FI(CUSTIN) DA('PROD.CUST.MASTER') SHR REUSE"
"ALLOC FI(CUSTNEW) DA('PROD.CUST.MASTER.NEW') MOD REUSE"
"ALLOC FI(REGLIST) DA('"region".REGIONS') SHR REUSE"

"EXECIO * DISKR CUSTIN (STEM rec. FINIS"
if rc <> 0 then exit 8
count = rec.0
"EXECIO" count "DISKW CUSTNEW (STEM rec. FINIS"
"EXECIO * DISKR REGLIST (STEM reg. FINIS"
"EXECIO 1 DISKW AUDITDD (STEM aud. FINIS"

/* Update the customers, then report on them. */
ADDRESS LINKMVS 'CUSTUPD'
ADDRESS ISPEXEC "SELECT PGM(CUSTRPT) PARM(SUMMARY)"

queue "RUN PROGRAM(BALSUM) PLAN(BALPLAN)"
queue "END"
"DSN SYSTEM(DB2P)"

step = 'NEXT' || region
interpret 'call' step
"%NOTIFY" region
call LOGRUN 'done'
"FREE FI(CUSTIN CUSTNEW REGLIST)"
exit 0

LOGRUN: procedure
  parse arg msg
  say msg
  return
