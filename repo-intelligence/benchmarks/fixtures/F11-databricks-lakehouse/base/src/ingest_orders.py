# Databricks notebook source
# MAGIC %md
# MAGIC # Ingest orders
# MAGIC Raw JSON from the landing zone into bronze.

# COMMAND ----------

# MAGIC %run ./common/config

# COMMAND ----------

catalog = dbutils.widgets.get("catalog")
storage_key = dbutils.secrets.get(scope="lakehouse", key="landing-sas")
LANDING = "abfss://landing@lakehousedata.dfs.core.windows.net/orders/"

raw = spark.read.format("json").load(LANDING)
raw.write.mode("append").saveAsTable(f"{catalog}.bronze.orders")

# COMMAND ----------

# MAGIC %sql
# MAGIC INSERT INTO main.silver.orders
# MAGIC SELECT o.*, c.region
# MAGIC FROM main.bronze.orders o
# MAGIC JOIN main.ref.countries c ON o.country_code = c.code
