# Warehouse loads (IBM DataStage)

DataStage jobs as the Designer and `istool` export them:

- `datastage/export/LoadCustomers.dsx`: a parallel job in the text export form. It reads
  a landing file, looks up the country in an Oracle table, cleans the rows in a
  Transformer (a valid and a reject output), sorts and de-duplicates them, and writes
  the warehouse table with insert and update. The reject file path is built from a job
  parameter. The export also holds the table definition of the target and a routine.
- `datastage/export/NightlyLoad.dsx`: a sequence job that checks the landing file with a
  routine, runs LoadCustomers and then LoadOrders, and mails support when either fails.
- `datastage/isx/orders.isx`: an Information Server archive holding LoadOrders in the
  XML export form, written by `../make_isx.py`.
