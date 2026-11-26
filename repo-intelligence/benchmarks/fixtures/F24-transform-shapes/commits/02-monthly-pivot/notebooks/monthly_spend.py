# Databricks notebook source
import pyspark.sql.functions as F

# COMMAND ----------

orders = spark.table("silver.orders")
wide = (orders.withColumn("month", F.date_format(F.col("order_date"), "yyyy-MM"))
              .groupBy("customer_id")
              .pivot("month")
              .agg(F.sum("amount")))
wide.write.mode("overwrite").saveAsTable("gold.monthly_spend")
