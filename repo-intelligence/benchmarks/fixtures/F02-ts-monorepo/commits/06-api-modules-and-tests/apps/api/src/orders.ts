import { prisma } from "./db";

export async function listOrders() {
  return prisma.order.findMany({ orderBy: { id: "desc" } });
}

export async function searchOrders(term: string) {
  return prisma.$queryRawUnsafe(`SELECT * FROM orders WHERE sku LIKE '%${term}%'`);
}
