"""The zos pack's vocabulary: platform terms and the constructs it looks for.

Keys follow contract section 11.1:

    Job {framework: "jcl", name}                 a JOB statement (zos:Job), a procedure (zos:Proc),
                                                 or the library member that holds a job whose JOB
                                                 statement has another name (zos:Member, ALIAS_OF)
    Task {framework: "jcl", job, name}           an EXEC statement (zos:Step); `job` is the job's or
                                                 the procedure's name
    Dataset {name}                               lower-cased data set name, as legacy.jcl keys it;
                                                 declared without a term (platform "mainframe"), so a
                                                 data set the JCL, COBOL and the CSD name is one entity
    Endpoint {component_root, protocol: "cics",  a CSD DEFINE TRANSACTION (zos:Transaction)
              method: "TRANSACTION", route}
    DataSchema {kind: "copybook", name: <member>}  a DCLGEN member (zos:Dclgen): a copybook, keyed
                                                 as the cobol pack keys one, so COPY <member> and
                                                 EXEC SQL INCLUDE <member> link to it (11.3 item 4)
    Field {table: "cobol:<member>", name}        the host structure and each host variable of a
                                                 DCLGEN member (zos:DclgenField), named as the cobol
                                                 pack names a copybook's items (11.1 item 3)
    Table {datastore: "default", name}           a DB2 DDL table, read by data.sql_ddl: table and
                                                 column names lower-cased (11.3 item 1)
    Program {platform: "db2-package", name: "<collection>.<package>"}  a DB2 package (zos:Db2Package)
    Program {platform: "db2-plan", name: <plan>} a DB2 application plan (zos:Db2Plan)

Constructs are counted on every occurrence. Those marked `unmapped` are read only far
enough to count them: output and printing, JES control statements, catalogue listings,
CICS resources that are not transactions or files.
"""

from __future__ import annotations

from repo_intel.compile.ontology import EntityKind as K
from repo_intel.platforms import Construct, Term

TERMS = (
    Term("zos:Job", K.JOB, "lossy", "a JCL job: the JOB statement; COND, IF/THEN/ELSE, INCLUDE and JCLLIB live in attributes"),
    Term("zos:Proc", K.JOB, "lossy", "a JCL procedure, cataloged (its own member) or in-stream (PROC ... PEND inside a job)"),
    Term("zos:Member", K.JOB, "lossy", "the library member that holds a job whose JOB statement has another name; ALIAS_OF that job"),
    Term("zos:Step", K.TASK, "lossy", "a job or procedure step: one EXEC statement; COND and IF conditions in attributes"),
    Term("zos:Transaction", K.ENDPOINT, "lossy", "a CICS transaction from a CSD DEFINE TRANSACTION: method TRANSACTION, the transaction id as route"),
    Term("zos:Dclgen", K.DATA_SCHEMA, "lossy", "a DCLGEN member, a copybook: the DECLARE TABLE and its host structure"),
    Term("zos:DclgenField", K.FIELD, "lossy", "the host structure or a host variable of a DCLGEN member; level, PIC, usage, offset, length and the column it holds"),
    Term("zos:Db2Package", K.PROGRAM, "lossy", "a DB2 package bound from a program's DBRM (BIND PACKAGE ... MEMBER)"),
    Term("zos:Db2Plan", K.PROGRAM, "lossy", "a DB2 application plan (BIND PLAN): the collections and packages it lists"),
)

_CONSTRUCTS = (
    # JCL statements
    ("job", "JOB statement", "entity:Job"),
    ("proc", "PROC statement of a cataloged procedure", "entity:Job"),
    ("in_stream_proc", "in-stream procedure: PROC ... PEND inside a job", "entity:Job"),
    ("member_alias", "a job whose JOB statement name differs from its member name", "relation:ALIAS_OF"),
    ("exec_pgm", "EXEC PGM=: the program a step runs", "relation:RUNS"),
    ("exec_utility", "EXEC PGM= of a system utility (SORT, IDCAMS, IEBGENER, IKJEFT01 ...)", "attribute:program"),
    ("exec_proc", "EXEC of a procedure", "relation:RUNS"),
    ("exec_dynamic", "EXEC PGM= naming the program through a symbolic or a referback", "unmapped"),
    ("exec_parm", "PARM= on EXEC: values are never read", "unmapped"),
    ("exec_override", "PARM.step=, COND.step= and other step overrides on a procedure call", "attribute:overrides"),
    ("cond", "COND= on JOB or EXEC", "attribute:cond"),
    ("if", "IF/THEN/ELSE/ENDIF construct", "behavior:if"),
    ("set", "SET statement", "attribute:symbols"),
    ("symbolic", "a symbolic parameter (&NAME) in a data set name or program", "attribute:parameterized"),
    ("include", "INCLUDE MEMBER=", "attribute:includes"),
    ("jcllib", "JCLLIB ORDER=", "attribute:jcllib"),
    ("output", "OUTPUT statement", "unmapped"),
    ("jes2", "JES2 control statement (/*JOBPARM, /*ROUTE, /*OUTPUT ...)", "unmapped"),
    ("jes3", "JES3 control statement (//*MAIN, //*NET, //*FORMAT ...)", "unmapped"),
    ("scheduler_directive", "scheduler directive in a comment (//*%OPC)", "unmapped"),
    ("other_statement", "CNTL, ENDCNTL, EXPORT, XMIT, COMMAND, SCHEDULE and other statements", "unmapped"),
    # DD statements
    ("dd_dsn", "DD DSN=: a catalogued data set a step reads or writes", "relation:READS_FROM"),
    ("dd_gdg", "a generation data group generation: DSN=name(+1), (0), (-1)", "attribute:gdg"),
    ("dd_member", "a partitioned data set member: DSN=library(member)", "relation:READS_FROM"),
    ("dd_temp", "a temporary data set (&&NAME) passed between steps", "unmapped"),
    ("dd_referback", "DSN=*.step.dd: a referback to an earlier DD", "relation:READS_FROM"),
    ("dd_concatenation", "an unnamed DD concatenated to the one before it", "relation:READS_FROM"),
    ("dd_override", "//procstep.ddname DD: a DD that overrides or adds to a procedure step", "relation:READS_FROM"),
    ("dd_in_stream", "DD * or DD DATA (with DLM=): in-stream data", "attribute:in_stream"),
    ("dd_implicit_sysin", "in-stream data with no DD statement (an implied SYSIN)", "attribute:in_stream"),
    ("dd_library", "STEPLIB, JOBLIB and other load or procedure libraries", "unmapped"),
    ("dd_system", "a system data set (SYS1., CEE., DSN. ...)", "unmapped"),
    ("dd_sysout", "DD SYSOUT=: printed output", "unmapped"),
    ("dd_dummy", "DD DUMMY or DSN=NULLFILE", "unmapped"),
    ("dd_path", "DD PATH=: a z/OS UNIX file", "relation:READS_FROM"),
    ("dd_other", "DD with DDNAME=, SUBSYS=, DYNAM or no data set", "unmapped"),
    ("dd_binding", "a DD of a step that runs a program: the DD name bound to a data set", "relation:READS_FROM"),
    ("control_member", "control cards in a library member (DSN=lib(member) on SYSIN, SYSTSIN, TOOLIN)", "unmapped"),
    # IDCAMS
    ("idcams_define_cluster", "IDCAMS DEFINE CLUSTER: a VSAM data set with its keys and record size", "entity:Dataset"),
    ("idcams_define_gdg", "IDCAMS DEFINE GDG: a generation data group base", "entity:Dataset"),
    ("idcams_define_aix", "IDCAMS DEFINE ALTERNATEINDEX: an alternate index over a cluster", "entity:Dataset"),
    ("idcams_define_path", "IDCAMS DEFINE PATH: a path over an alternate index", "relation:ALIAS_OF"),
    ("idcams_define_other", "IDCAMS DEFINE NONVSAM, ALIAS, USERCATALOG, PAGESPACE", "unmapped"),
    ("idcams_delete", "IDCAMS DELETE", "relation:WRITES_TO"),
    ("idcams_repro", "IDCAMS REPRO: a copy between data sets", "relation:READS_FROM"),
    ("idcams_condition", "IDCAMS IF LASTCC / MAXCC and SET", "unmapped"),
    ("idcams_other", "IDCAMS LISTCAT, PRINT, ALTER, VERIFY, EXPORT, IMPORT, BLDINDEX", "unmapped"),
    # DFSORT and ICETOOL
    ("sort_fields", "DFSORT SORT FIELDS=(p,l,f,o,...)", "transform:sort"),
    ("sort_copy", "DFSORT SORT FIELDS=COPY or OPTION COPY", "transform:read"),
    ("merge_fields", "DFSORT MERGE FIELDS=", "transform:union"),
    ("sort_include", "DFSORT INCLUDE COND= or OMIT COND=", "transform:filter"),
    ("sort_reformat", "DFSORT INREC or OUTREC FIELDS=/BUILD= with a field list", "transform:project"),
    ("sort_reformat_other", "DFSORT INREC or OUTREC OVERLAY=, IFTHEN=, FINDREP=", "unmapped"),
    ("sort_sum", "DFSORT SUM FIELDS=(p,l,f,...)", "transform:aggregate"),
    ("sort_sum_none", "DFSORT SUM FIELDS=NONE: one record per key", "transform:deduplicate"),
    ("sort_outfil", "DFSORT OUTFIL: an output file, with its INCLUDE or OMIT", "transform:filter"),
    ("sort_joinkeys", "DFSORT JOINKEYS, JOIN and REFORMAT", "transform:join"),
    ("sort_symbolic_field", "a DFSORT field named through SYMNAMES", "unmapped"),
    ("sort_option", "DFSORT OPTION, RECORD, ALTSEQ, MODS, END and other control statements", "unmapped"),
    ("icetool_copy", "ICETOOL COPY", "transform:read"),
    ("icetool_sort", "ICETOOL SORT", "transform:sort"),
    ("icetool_select", "ICETOOL SELECT FIRST, LAST, NODUPS, FIRSTDUP, LASTDUP", "transform:deduplicate"),
    ("icetool_other", "ICETOOL COUNT, DISPLAY, OCCUR, STATS, UNIQUE, SPLICE, MODE and others", "unmapped"),
    # TSO in batch (IKJEFT01) and DB2
    ("tso_run_program", "DSN RUN PROGRAM(P): the DB2 program a step runs", "relation:RUNS"),
    ("tso_plan", "PLAN(X) on RUN PROGRAM", "attribute:plan"),
    ("tso_exec", "%NAME, EX or EXEC of a REXX exec or CLIST", "relation:RUNS"),
    ("tso_dsn", "DSN SYSTEM(...) and END", "unmapped"),
    ("tso_command", "another TSO command in SYSTSIN", "unmapped"),
    ("bind_package", "BIND PACKAGE(collection) MEMBER(dbrm)", "entity:Program"),
    ("bind_plan", "BIND PLAN(plan)", "entity:Program"),
    ("bind_pklist", "PKLIST entry of a plan: collection.* or collection.package", "relation:CONTAINS"),
    ("bind_member", "MEMBER(dbrm): the program a package or plan is bound from", "relation:DEPENDS_ON"),
    ("bind_option", "OWNER, QUALIFIER, ACTION, ISOLATION and other bind options", "unmapped"),
    ("bind_other", "REBIND, FREE, BIND PACKAGE COPY and DB2 commands", "unmapped"),
    # CICS CSD (DFHCSDUP)
    ("csd_transaction", "DEFINE TRANSACTION", "entity:Endpoint"),
    ("csd_program", "DEFINE PROGRAM: its language, recorded on the transactions that name it", "attribute:program_language"),
    ("csd_file", "DEFINE FILE: a CICS file name standing for its DSNAME", "relation:ALIAS_OF"),
    ("csd_db2", "DEFINE DB2ENTRY or DB2TRAN: the plan a transaction uses", "attribute:plan"),
    ("csd_mapset", "DEFINE MAPSET", "unmapped"),
    ("csd_queue", "DEFINE TDQUEUE, TSMODEL, MQCONN, MQQUEUE", "unmapped"),
    ("csd_web", "DEFINE URIMAP, WEBSERVICE, PIPELINE, TCPIPSERVICE, ATOMSERVICE", "unmapped"),
    ("csd_connection", "DEFINE CONNECTION, SESSIONS, IPCONN, TERMINAL, TYPETERM", "unmapped"),
    ("csd_other", "another CSD resource (PROFILE, TRANCLASS, LIBRARY, DB2CONN, JVMSERVER, BUNDLE ...)", "unmapped"),
    ("csd_group", "ADD GROUP ... LIST, and other DFHCSDUP commands", "unmapped"),
    # DCLGEN
    ("dclgen_table", "DCLGEN DECLARE TABLE", "entity:DataSchema"),
    ("dclgen_field", "the record or a host variable of a DCLGEN COBOL structure", "entity:Field"),
    ("dclgen_indicator", "a DCLGEN indicator variable structure", "unmapped"),
    ("dclgen_other_language", "a DCLGEN host structure in PL/I, C or assembler", "unmapped"),
    # DB2 DDL outside .sql files
    ("ddl_table", "CREATE TABLE", "entity:Table"),
    ("ddl_view", "CREATE VIEW", "entity:DataSchema"),
    ("ddl_index", "CREATE INDEX", "unmapped"),
    ("ddl_storage", "CREATE DATABASE, TABLESPACE, STOGROUP, AUXILIARY TABLE", "unmapped"),
    ("ddl_alias", "CREATE ALIAS or SYNONYM", "unmapped"),
    ("ddl_grant", "GRANT or REVOKE", "unmapped"),
    ("ddl_comment", "COMMENT ON or LABEL ON", "unmapped"),
    ("ddl_other", "ALTER, DROP and other SQL statements", "unmapped"),
)

CONSTRUCTS = tuple(Construct(f"zos:{cid}", description, modelled_as) for cid, description, modelled_as in _CONSTRUCTS)
