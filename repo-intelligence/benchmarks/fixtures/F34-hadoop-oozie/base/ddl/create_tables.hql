-- Warehouse tables of the daily orders application.
CREATE DATABASE IF NOT EXISTS mart;

CREATE TABLE IF NOT EXISTS mart.daily_summary (
  order_date STRING,
  region STRING,
  orders BIGINT,
  total DECIMAL(12,2)
)
PARTITIONED BY (dt STRING)
STORED AS ORC;

CREATE TABLE IF NOT EXISTS dim.customers (
  customer_id BIGINT,
  name STRING,
  region STRING,
  email STRING,
  signup_date DATE
)
STORED AS ORC;

CREATE VIEW IF NOT EXISTS mart.v_daily_totals AS
SELECT order_date, SUM(total) AS total FROM mart.daily_summary GROUP BY order_date;
