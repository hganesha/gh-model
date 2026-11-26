# Order desk requirements

Signed off by the order desk, 2026-01.

## Capture

- REQ-1: Every order must carry a status from the order status list, held in `orders.status`.
- REQ-2: The desk shall list open orders from `orders`, newest first.
- REQ-3 A new order starts as a draft.

## Customers

The nightly export should include every row of sales.customers.

Customer names are free text.
