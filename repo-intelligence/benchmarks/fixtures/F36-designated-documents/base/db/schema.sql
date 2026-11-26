CREATE TABLE orders (
    order_id INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL,
    status VARCHAR(20) NOT NULL,
    amount DECIMAL(10, 2)
);

CREATE TABLE sales.customers (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    region CHAR(2)
);
