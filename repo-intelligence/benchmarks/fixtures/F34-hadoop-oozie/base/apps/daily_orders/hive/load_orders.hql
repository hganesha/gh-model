-- Load the day's orders into the open and closed order tables.
SET hive.exec.dynamic.partition.mode=nonstrict;
USE staging;

CREATE EXTERNAL TABLE IF NOT EXISTS raw_orders (
  order_id BIGINT,
  customer_id BIGINT,
  order_date STRING,
  amount DECIMAL(10,2),
  status STRING
)
PARTITIONED BY (dt STRING)
ROW FORMAT DELIMITED FIELDS TERMINATED BY ','
STORED AS TEXTFILE
LOCATION '${nameNode}/data/raw/orders';

MSCK REPAIR TABLE raw_orders;

FROM raw_orders r
INSERT OVERWRITE TABLE mart.open_orders PARTITION (dt='${hivevar:run_date}')
  SELECT r.order_id, r.customer_id, r.order_date, r.amount WHERE r.status = 'OPEN' AND r.dt = '${hivevar:run_date}'
INSERT OVERWRITE TABLE mart.closed_orders PARTITION (dt='${hivevar:run_date}')
  SELECT r.order_id, r.amount WHERE r.status = 'CLOSED' AND r.dt = '${hivevar:run_date}';

-- The latest version of each order.
INSERT OVERWRITE TABLE ${hivevar:mart_db}.latest_orders
SELECT * FROM (
  SELECT o.*, ROW_NUMBER() OVER (PARTITION BY order_id ORDER BY dt DESC) rn FROM raw_orders o
) t WHERE rn = 1;
