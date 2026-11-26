// The shop front end's calls to the orders service. The service is a separate
// repository; nothing in this one serves these paths.

const logLevel = process.env.LOG_LEVEL;

export async function listOrders() {
  const response = await fetch("/api/orders");
  return response.json();
}

export async function placeOrder(order: { customerId: number; total: number }) {
  const response = await fetch("/api/orders", { method: "POST", body: JSON.stringify(order) });
  return response.json();
}

export async function getOrder(id: number) {
  const response = await fetch(`/api/orders/${id}`);
  return response.json();
}

export async function listCustomers() {
  // No member of the solution serves this path.
  const response = await fetch("/api/customers");
  return response.json();
}

export function debug(message: string) {
  if (logLevel === "debug") {
    console.log(message);
  }
}
