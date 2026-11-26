# Databricks notebook source
from pyspark.sql import Window
import pyspark.sql.functions as F

# COMMAND ----------

orders = spark.read.table("raw.orders")
latest_first = Window.partitionBy("order_id").orderBy(F.col("updated_at").desc())
latest = (orders.withColumn("rn", F.row_number().over(latest_first))
                .filter(F.col("rn") == 1)
                .drop("rn"))

# COMMAND ----------

clean = (latest.withColumn("customer_name", F.trim(F.col("customer_name")))
               .withColumn("status", F.upper(F.col("status")))
               .withColumn("discount", F.coalesce(F.col("discount"), F.lit(0)))
               .withColumn("order_date", F.to_date(F.col("order_ts")))
               .withColumnRenamed("cust", "customer_id"))
clean.write.mode("overwrite").saveAsTable("silver.orders")

# COMMAND ----------

customers = spark.table("silver.customers")
summary = (clean.join(customers, on="customer_id", how="left")
                .groupBy("region")
                .agg(F.sum("amount").alias("total_amount"), F.count("*").alias("order_count")))
summary.write.mode("overwrite").saveAsTable("gold.region_summary")

# COMMAND ----------

# MAGIC %sql
# MAGIC insert overwrite gold.top_customers
# MAGIC select customer_id, sum(amount) as total_amount
# MAGIC from silver.orders
# MAGIC group by customer_id
