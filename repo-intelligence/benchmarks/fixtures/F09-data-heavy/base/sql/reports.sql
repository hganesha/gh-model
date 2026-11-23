CREATE VIEW customer_totals AS
SELECT c.id, c.email, sum(o.total_cents) AS total_cents
FROM customers c JOIN orders o ON o.customer_id = c.id
GROUP BY c.id, c.email;
