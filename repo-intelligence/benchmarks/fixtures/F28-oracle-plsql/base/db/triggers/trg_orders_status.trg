CREATE OR REPLACE TRIGGER trg_orders_status
  BEFORE UPDATE OF status ON orders
  FOR EACH ROW
  WHEN (new.status <> old.status)
BEGIN
  IF :old.status = 'CLOSED' THEN
    RAISE_APPLICATION_ERROR(-20010, 'A closed order cannot change status');
  END IF;
  :new.updated_at := SYSDATE;
  INSERT INTO order_status_history (order_id, old_status, new_status, changed_at)
  VALUES (:new.order_id, :old.status, :new.status, SYSDATE);
END;
/
