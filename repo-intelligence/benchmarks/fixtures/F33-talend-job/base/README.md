# Warehouse loads (Talend)

A Talend project folder, `DWH/`, as Talend Studio writes it:

- `process/Customers/LoadCustomers_0.1.item`: reads the landing file, looks the country
  up in an Oracle table in a tMap (a valid and a reject output), sorts and de-duplicates
  the rows and upserts the warehouse table. The reject file path is built from a context
  variable.
- `process/Orders/LoadOrders_0.1.item`: totals the orders of a PostgreSQL table per
  customer into a work file.
- `process/Nightly_0.1.item`: runs LoadCustomers, then LoadOrders when it succeeded, and
  mails support when it failed.
- `code/routines/DateUtils_0.1.item`: a routine's Java source.

Each item has its `.properties` next to it.
