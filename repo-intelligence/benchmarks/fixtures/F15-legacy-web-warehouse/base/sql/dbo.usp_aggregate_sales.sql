CREATE PROCEDURE [dbo].[usp_aggregate_sales]
AS
BEGIN
    SET NOCOUNT ON;
    TRUNCATE TABLE dbo.sales_agg;
    INSERT INTO dbo.sales_agg (sale_date, region, amount)
    SELECT s.sale_date, st.region, SUM(s.amount)
    FROM stg.sales_clean s
    JOIN dbo.stores st ON st.store_id = s.store_id
    GROUP BY s.sale_date, st.region;
END
GO
