-- Orders and totals per day and region.
INSERT OVERWRITE TABLE mart.daily_summary PARTITION (dt='${hivevar:run_date}')
SELECT o.order_date, c.region, COUNT(*) AS orders, SUM(o.amount) AS total
FROM mart.open_orders o
JOIN dim.customers c ON o.customer_id = c.customer_id
WHERE o.dt = '${hivevar:run_date}'
GROUP BY o.order_date, c.region;

ANALYZE TABLE mart.daily_summary PARTITION (dt='${hivevar:run_date}') COMPUTE STATISTICS;
