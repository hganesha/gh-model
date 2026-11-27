# Accounts batch

Daily account posting on z/OS: the job in `jcl/ACCTDLY.jcl` reloads the VSAM master,
extracts the open accounts through an in-stream procedure, summarises them with DFSORT,
posts them to DB2 through IKJEFT01 and archives the master with the cataloged procedure in
`jcl/ACCTARCH.prc`. The CICS definitions, the DCLGEN member, the BIND cards, the DB2 DDL
and a DFSORT control member are exported under `.rkp/inputs/zos/`.
