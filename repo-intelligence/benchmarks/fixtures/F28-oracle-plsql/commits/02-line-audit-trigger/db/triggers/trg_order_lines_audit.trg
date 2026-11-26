CREATE OR REPLACE TRIGGER trg_order_lines_audit
  AFTER INSERT OR DELETE ON order_lines
  REFERENCING NEW AS nw OLD AS od
  FOR EACH ROW
BEGIN
  IF INSERTING THEN
    INSERT INTO order_line_audit (order_id, item_id, action, changed_at)
    VALUES (:nw.order_id, :nw.item_id, 'ADDED', SYSDATE);
  ELSE
    INSERT INTO order_line_audit (order_id, item_id, action, changed_at)
    VALUES (:od.order_id, :od.item_id, 'REMOVED', SYSDATE);
  END IF;
END;
/
