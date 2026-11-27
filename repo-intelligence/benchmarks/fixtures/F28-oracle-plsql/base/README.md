# Order desk database (Oracle PL/SQL)

The database side of an order desk, kept as source files with no build file:

- `db/packages/order_api.pks` and `.pkb`: a package spec and body. The body has a
  package cursor, an autonomous logging procedure, SELECT INTO, INSERT with RETURNING,
  a cursor FOR loop, a MERGE over a synonym for a table across a database link,
  BULK COLLECT, two FORALL statements and dynamic SQL built at run time.
- `db/procedures/purge_stale_carts.prc`: a standalone procedure. The `.prc` suffix is
  also used for JCL procedures; `jcl/NIGHTLY.prc` is one, and is not Oracle.
- `db/functions/customer_tier.fnc`: a standalone function with a searched CASE.
- `db/triggers/trg_orders_status.trg`: a row trigger with a WHEN clause.
- `db/install/grants_and_jobs.sql`: a SQL*Plus install script with two database links (one
  connecting as a fixed user with a made-up password, which is planted test data), a
  synonym, object, system and role grants, two DBMS_SCHEDULER jobs and a DBMS_JOB job.
