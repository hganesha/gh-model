#!/bin/sh
# Mark the day's summary as published.
set -e
hdfs dfs -touchz "/data/mart/daily_summary/dt=$1/_PUBLISHED"
