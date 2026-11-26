CREATE OR REPLACE PROCEDURE purge_stale_carts (p_days IN PLS_INTEGER DEFAULT 30) AS
  v_deleted PLS_INTEGER;
BEGIN
  DELETE FROM carts WHERE updated_at < SYSDATE - p_days;
  v_deleted := SQL%ROWCOUNT;
  DBMS_OUTPUT.PUT_LINE('Purged ' || v_deleted || ' carts');
  COMMIT;
END purge_stale_carts;
/
