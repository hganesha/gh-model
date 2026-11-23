export interface Order {
  id: number;
  sku: string;
  quantity: number;
  note?: string;
}

export const ORDER_QUEUE = "orders";
