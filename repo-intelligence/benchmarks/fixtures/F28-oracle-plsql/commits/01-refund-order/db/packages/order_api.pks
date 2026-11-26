CREATE OR REPLACE PACKAGE order_api AUTHID DEFINER AS
  -- The order desk: placing, pricing, closing and archiving orders.
  c_open CONSTANT VARCHAR2(10) := 'OPEN';
  e_blocked_customer EXCEPTION;

  PROCEDURE place_order (
    p_customer_id IN  NUMBER,
    p_item_id     IN  NUMBER,
    p_qty         IN  NUMBER,
    p_order_id    OUT NUMBER);

  FUNCTION order_total (p_order_id IN NUMBER) RETURN NUMBER;

  PROCEDURE close_order (p_order_id IN NUMBER);

  PROCEDURE load_remote_customers;

  PROCEDURE archive_orders (p_before IN DATE);

  PROCEDURE refund_order (p_order_id IN NUMBER, p_reason IN VARCHAR2);
END order_api;
/
SHOW ERRORS
