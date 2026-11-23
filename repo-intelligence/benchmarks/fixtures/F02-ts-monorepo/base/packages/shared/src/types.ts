export interface Order {
  id: number;
  sku: string;
  quantity: number;
}

export const ORDER_QUEUE = "orders";
