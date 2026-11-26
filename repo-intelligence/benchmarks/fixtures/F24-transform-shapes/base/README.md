# Order pipelines

The same order data moves through three toolchains:

- `sql/` loads the staging tables: the latest version of each order, a merge into the
  customer table, and a daily total per region.
- `sas/` builds the monthly analysis: sorted orders, one row per customer, customers with
  no orders, totals per customer, a month-by-month layout and a summary.
- `notebooks/` and `jobs/` run the PySpark side: cleaned orders, a regional summary and
  the archive union.
