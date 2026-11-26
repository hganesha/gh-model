"""Union the live and archived orders, one row per order."""

from pyspark.sql import SparkSession

spark = SparkSession.builder.appName("archive_orders").getOrCreate()

live = spark.read.table("silver.orders")
archived = spark.read.parquet("/mnt/archive/orders")
everything = live.unionByName(archived, allowMissingColumns=True)
everything.dropDuplicates(["order_id"]).write.mode("overwrite").saveAsTable("silver.orders_all")


def load_region(df, region):
    """One region's orders, written to a table named after it."""
    df.filter(df.region == region).write.saveAsTable(f"silver.orders_{region}")
