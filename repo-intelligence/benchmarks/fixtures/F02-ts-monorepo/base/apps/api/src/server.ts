import express from "express";
import { PrismaClient } from "@prisma/client";
import type { Order } from "@acme/shared";

const app = express();
const prisma = new PrismaClient();
app.use(express.json());

app.get("/orders", async (_req, res) => {
  const orders: Order[] = await prisma.order.findMany();
  res.json(orders);
});

app.post("/orders", async (req, res) => {
  const order = await prisma.order.create({ data: { sku: req.body.sku, quantity: req.body.quantity } });
  res.status(201).json(order);
});

app.listen(Number(process.env.PORT ?? 4000));
