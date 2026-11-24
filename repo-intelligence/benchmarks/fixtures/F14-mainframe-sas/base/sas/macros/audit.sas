%macro audit_log(table=);
  proc sql;
    insert into rpt.audit_log (table_name, loaded_at)
    values ("&table", datetime());
  quit;
%mend audit_log;
