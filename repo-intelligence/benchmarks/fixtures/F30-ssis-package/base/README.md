# Sales load

An SSIS project that loads the day's order file into staging, summarises it by region
into the warehouse and publishes the marts.

- `etl/SalesLoad/LoadSales.dtsx` checks the inbox, truncates staging, loads the order
  file through a customer lookup, derived columns and a value split, summarises the
  staged orders and runs the publish package.
- `etl/SalesLoad/Publish.dtsx` refreshes the mart history, copies the regional summary
  to the table the `MartTable` variable names and rebuilds its indexes.
- `etl/SalesLoad/Warehouse.conmgr` is the project connection to the warehouse.
- `etl/SalesLoad/Project.params` holds the project parameters.
