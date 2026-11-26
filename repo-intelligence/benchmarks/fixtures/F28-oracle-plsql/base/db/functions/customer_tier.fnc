CREATE OR REPLACE FUNCTION customer_tier (p_customer_id IN NUMBER) RETURN VARCHAR2 IS
  v_spend NUMBER;
BEGIN
  SELECT NVL(SUM(o.total), 0)
    INTO v_spend
    FROM orders o
   WHERE o.customer_id = p_customer_id
     AND o.status = 'CLOSED';

  CASE
    WHEN v_spend >= 10000 THEN RETURN 'GOLD';
    WHEN v_spend >= 1000 THEN RETURN 'SILVER';
    ELSE RETURN 'BRONZE';
  END CASE;
END customer_tier;
/
