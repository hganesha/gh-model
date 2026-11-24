import { ServiceBusClient } from "@azure/service-bus";
import { PubSub } from "@google-cloud/pubsub";

const INVENTORY_TOPIC = "inventory";

const bus = new ServiceBusClient(process.env.SERVICEBUS_CONNECTION!);
const pubsub = new PubSub();

export async function run(): Promise<void> {
  const receiver = bus.createReceiver("billing");
  receiver.subscribe({
    processMessage: async (message) => {
      await pubsub.topic(INVENTORY_TOPIC).publishMessage({ json: message.body });
    },
    processError: async () => {},
  });
}
