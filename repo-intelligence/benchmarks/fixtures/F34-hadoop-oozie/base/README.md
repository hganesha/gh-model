# Daily orders (Hadoop)

One Oozie application, `apps/daily_orders/`: a workflow that imports the day's orders
from a sales database with Sqoop, loads them with a Hive script, then in parallel cleans
the customer extract with Pig and builds a daily summary with a second Hive script, and,
when the summary is not empty, exports it to a reporting database and runs a shell script
that marks it published. Any failure sends an email and stops the run. A coordinator starts
the workflow every day from a landing dataset.

Also here: `ddl/create_tables.hql` (warehouse tables and a view) and a Sqoop options file
that imports the product catalogue into Hive.
