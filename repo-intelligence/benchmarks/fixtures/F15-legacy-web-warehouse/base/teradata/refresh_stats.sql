REPLACE PROCEDURE edw.refresh_sales_stats()
BEGIN
  DELETE FROM edw.sales_daily WHERE sale_date = CURRENT_DATE;
  INSERT INTO edw.sales_daily (sale_date, store_id, revenue)
  SELECT sale_date, store_id, SUM(amount) FROM edw.sales WHERE sale_date = CURRENT_DATE GROUP BY 1, 2;
END;
