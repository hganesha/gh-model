"""The REXX and CLIST pack's vocabulary: platform terms and the constructs it looks for.

Keys: an exec or CLIST (one member) is `Program {platform: "tso", name: <member>}`; a program
it runs is `Program {platform: "mainframe", name}` (contract 11.1 item 2), another exec it
runs is `Program {platform: "tso", name}`; a dataset it allocates and reads or writes is
`Dataset {name}` with `attributes.platform` "mainframe", as `legacy.jcl` declares one.
"""

from __future__ import annotations

from repo_intel.compile.ontology import EntityKind as K
from repo_intel.platforms import Construct, Term

TERMS = (
    Term("rexx:Exec", K.PROGRAM, "lossy", "a REXX exec or CLIST (one member): the environments it addresses, its allocations and files in attributes"),
)

_CONSTRUCTS = (
    # Host commands.
    ("address", "ADDRESS of an environment (TSO, MVS, ISPEXEC, LINKMVS ...)", "unmapped"),
    ("address_dynamic", "ADDRESS VALUE: an environment named at run time", "unmapped"),
    ("command", "a command to an environment this pack does not read (CONSOLE, SYSCALL ...)", "unmapped"),
    ("dynamic_command", "a command whose name is built at run time", "unmapped"),
    ("tso_command", "another TSO command (LISTDS, DELETE, SEND ...)", "unmapped"),
    ("tso_call", "TSO CALL of a load module: `CALL 'LIB(MEMBER)'`", "relation:CALLS"),
    ("tso_exec", "TSO EXEC, EX or %name of another exec or CLIST", "relation:CALLS"),
    ("link_program", "ADDRESS LINKMVS, LINKPGM, ATTCHMVS ... of a program", "relation:CALLS"),
    ("ispexec_select", "ISPEXEC SELECT PGM() or CMD()", "relation:CALLS"),
    ("ispexec", "another ISPF service (DISPLAY, LIBDEF, VGET, FTINCL ...)", "unmapped"),
    ("isredit", "an ISPF edit macro command", "unmapped"),
    ("dsn", "the DSN command (DB2)", "unmapped"),
    ("dsn_run", "DSN RUN PROGRAM() of a program", "relation:CALLS"),
    ("alloc", "ALLOC FI() DA(): the DD name bound to datasets", "attribute:allocations"),
    ("free", "FREE of a DD name", "unmapped"),
    ("execio", "EXECIO DISKR, DISKRU or DISKW of a DD name", "relation:READS_FROM"),
    ("submit", "SUBMIT of a job", "attribute:submits"),
    ("sql", "ADDRESS DSNREXX EXECSQL of a static statement", "relation:READS_FROM"),
    ("sql_dynamic", "EXECSQL of a statement built at run time, or PREPARE and EXECUTE", "unmapped"),
    ("sql_other", "EXECSQL of a cursor or connection statement, and CONNECT or DISCONNECT", "unmapped"),
    # REXX instructions.
    ("assignment", "an assignment", "unmapped"),
    ("call_internal", "CALL of a routine or a function in the exec", "unmapped"),
    ("call_external", "CALL of an external routine: another exec", "relation:CALLS"),
    ("function_external", "a function call to an external function: another exec", "relation:CALLS"),
    ("control", "IF, DO, SELECT, WHEN, OTHERWISE, ITERATE, LEAVE, NOP, END", "unmapped"),
    ("exit", "EXIT or RETURN", "unmapped"),
    ("interpret", "INTERPRET: a statement built at run time", "unmapped"),
    ("label", "a label (an internal routine or a SIGNAL target)", "unmapped"),
    ("other_instruction", "PROCEDURE, NUMERIC, OPTIONS, TRACE, DROP, UPPER and the rest", "unmapped"),
    ("parse", "PARSE, ARG or PULL", "unmapped"),
    ("queue", "PUSH or QUEUE onto the data stack", "unmapped"),
    ("say", "SAY", "unmapped"),
    ("signal", "SIGNAL of a label or a condition", "unmapped"),
    # CLIST statements.
    ("clist_control", "CLIST IF, DO, SELECT, GOTO, EXIT, RETURN, SYSCALL, END", "unmapped"),
    ("clist_file", "CLIST OPENFILE of a DD name", "relation:READS_FROM"),
    ("clist_file_io", "CLIST GETFILE, PUTFILE and CLOSFILE", "unmapped"),
    ("clist_other", "CLIST PROC, CONTROL, GLOBAL, WRITE, READ, DATA, ERROR, ATTN and the rest", "unmapped"),
    ("clist_set", "CLIST SET", "unmapped"),
)

CONSTRUCTS = tuple(sorted((Construct(f"rexx:{name}", text, modelled) for name, text, modelled in _CONSTRUCTS), key=lambda c: c.id))
