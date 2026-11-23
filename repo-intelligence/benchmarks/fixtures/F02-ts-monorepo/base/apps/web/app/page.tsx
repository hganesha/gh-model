import { Button } from "@acme/ui";
import type { Order } from "@acme/shared";

export default async function Page() {
  const orders: Order[] = await fetch(`${process.env.API_URL}/orders`).then((r) => r.json());
  return <main>{orders.map((o) => <Button key={o.id}>{o.sku}</Button>)}</main>;
}
