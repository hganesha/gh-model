import { Worker } from "bullmq";
import { ORDER_QUEUE, type Order } from "@acme/shared";

new Worker<Order>(ORDER_QUEUE, async (job) => {
  console.log("fulfilling", job.data.sku);
});
