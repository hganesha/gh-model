from delta.tables import DeltaTable
from pyspark.sql import SparkSession, functions as F

spark = SparkSession.builder.getOrCreate()

orders = spark.read.table("main.silver.orders")
daily = orders.groupBy("order_date").agg(F.sum("amount").alias("revenue"))
daily.write.mode("overwrite").saveAsTable("main.gold.daily_revenue")

customers = orders.select("customer_id", "region").distinct()
(
    DeltaTable.forName(spark, "main.gold.customers")
    .alias("t")
    .merge(customers.alias("s"), "t.customer_id = s.customer_id")
    .whenNotMatchedInsertAll()
    .execute()
)

table_name = f"main.gold.{spark.conf.get('lakehouse.extra_table')}"
spark.read.table(spark.conf.get("lakehouse.audit_table")).count()
