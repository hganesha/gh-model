# Sales (PowerCenter)

The SALES folder of the ETL repository, exported from the Repository Manager.

- `informatica/exports/sales_folder.xml` holds the source and target definitions, the
  `m_load_sales` mapping (orders through a customer lookup, an expression, a filter and
  a router into two warehouse tables), the `m_summarise` mapping (a SQL override, an
  aggregator and a sorter) and the `wf_daily_sales` workflow that runs them.
- `informatica/params/wf_daily_sales.par` is the workflow's parameter file.
