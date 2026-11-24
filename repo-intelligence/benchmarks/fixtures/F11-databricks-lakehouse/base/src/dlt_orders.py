# Databricks notebook source
import dlt
from pyspark.sql import functions as F


@dlt.table(name="orders_raw")
def orders_raw():
    return spark.readStream.format("cloudFiles").option("cloudFiles.format", "json").load("/Volumes/main/landing/orders")


@dlt.table
def orders_clean():
    return dlt.read("orders_raw").where(F.col("amount") > 0)
