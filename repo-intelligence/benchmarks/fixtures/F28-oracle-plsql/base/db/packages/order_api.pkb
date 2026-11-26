CREATE OR REPLACE PACKAGE BODY order_api AS

  CURSOR c_lines (p_order_id NUMBER) IS
    SELECT l.item_id, l.qty, l.unit_price
      FROM order_lines l
     WHERE l.order_id = p_order_id;

  PROCEDURE log_event (p_order_id IN NUMBER, p_event IN VARCHAR2) IS
    PRAGMA AUTONOMOUS_TRANSACTION;
  BEGIN
    INSERT INTO order_events (order_id, event, created_at)
    VALUES (p_order_id, p_event, SYSDATE);
    COMMIT;
  END log_event;

  PROCEDURE place_order (
    p_customer_id IN  NUMBER,
    p_item_id     IN  NUMBER,
    p_qty         IN  NUMBER,
    p_order_id    OUT NUMBER) IS
    v_price  items.unit_price%TYPE;
    v_status customers.status%TYPE;
  BEGIN
    IF p_qty <= 0 THEN
      RAISE_APPLICATION_ERROR(-20001, 'Quantity must be positive');
    END IF;

    SELECT status INTO v_status FROM customers WHERE customer_id = p_customer_id;
    IF v_status = 'BLOCKED' THEN
      RAISE e_blocked_customer;
    END IF;

    SELECT unit_price INTO v_price FROM items WHERE item_id = p_item_id;

    INSERT INTO orders (order_id, customer_id, status, created_at)
    VALUES (order_seq.NEXTVAL, p_customer_id, c_open, SYSDATE)
    RETURNING order_id INTO p_order_id;

    INSERT INTO order_lines (order_id, item_id, qty, unit_price)
    VALUES (p_order_id, p_item_id, p_qty, v_price);

    log_event(p_order_id, 'PLACED');
  EXCEPTION
    WHEN NO_DATA_FOUND THEN
      RAISE_APPLICATION_ERROR(-20002, 'Unknown customer or item');
  END place_order;

  FUNCTION order_total (p_order_id IN NUMBER) RETURN NUMBER IS
    v_total NUMBER := 0;
  BEGIN
    FOR r IN c_lines(p_order_id) LOOP
      v_total := v_total + r.qty * r.unit_price;
    END LOOP;
    RETURN v_total;
  END order_total;

  PROCEDURE close_order (p_order_id IN NUMBER) IS
  BEGIN
    UPDATE orders
       SET status = 'CLOSED',
           total  = order_total(p_order_id)
     WHERE order_id = p_order_id;
    IF SQL%ROWCOUNT = 0 THEN
      RAISE_APPLICATION_ERROR(-20003, 'No such order');
    END IF;
    log_event(p_order_id, 'CLOSED');
  END close_order;

  PROCEDURE load_remote_customers IS
    v_remote PLS_INTEGER;
  BEGIN
    SELECT COUNT(*) INTO v_remote FROM customers@crm;
    MERGE INTO customers c
    USING (SELECT customer_id, name, status FROM crm_customers) s
       ON (c.customer_id = s.customer_id)
     WHEN MATCHED THEN
       UPDATE SET c.name = s.name, c.status = s.status
     WHEN NOT MATCHED THEN
       INSERT (customer_id, name, status) VALUES (s.customer_id, s.name, s.status);
    log_event(NULL, 'CUSTOMERS SYNCED: ' || v_remote);
    COMMIT;
  END load_remote_customers;

  PROCEDURE archive_orders (p_before IN DATE) IS
    TYPE t_ids IS TABLE OF orders.order_id%TYPE;
    v_ids   t_ids;
    v_table VARCHAR2(30) := 'orders_archive_' || TO_CHAR(p_before, 'YYYY');
  BEGIN
    SELECT order_id BULK COLLECT INTO v_ids
      FROM orders
     WHERE status = 'CLOSED' AND created_at < p_before;

    EXECUTE IMMEDIATE 'CREATE TABLE ' || v_table || ' AS SELECT * FROM orders WHERE 1 = 0';

    FORALL i IN 1 .. v_ids.COUNT
      EXECUTE IMMEDIATE 'INSERT INTO ' || v_table || ' SELECT * FROM orders WHERE order_id = :1' USING v_ids(i);

    FORALL i IN 1 .. v_ids.COUNT
      DELETE FROM orders WHERE order_id = v_ids(i);

    COMMIT;
  EXCEPTION
    WHEN OTHERS THEN
      ROLLBACK;
      RAISE;
  END archive_orders;

END order_api;
/
SHOW ERRORS
