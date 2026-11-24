from pyspark.sql import SparkSession

spark = SparkSession.builder.appName("extract_orders").getOrCreate()
orders = spark.read.format("jdbc").option("dbtable", "public.orders").load()
orders.write.mode("append").saveAsTable("raw.orders")
