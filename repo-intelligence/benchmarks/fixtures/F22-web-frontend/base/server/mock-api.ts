// A mock of the order API for local development (npm run mock-api).
import express from "express";

const app = express();
app.use(express.json());

const orders: { id: string; customer: string; status: string }[] = [];

app.get("/api/orders", (_req, res) => {
  res.json(orders);
});

app.post("/api/orders", (req, res) => {
  const order = { id: String(orders.length + 1), ...req.body };
  orders.push(order);
  res.status(201).json(order);
});

app.post("/api/checkout", (_req, res) => {
  res.status(202).json({ accepted: true });
});

app.listen(4000);
