import axios from "axios";
import axiosRetry from "axios-retry";
import OpenAI from "openai";

const client = new OpenAI();
const MAX_TURNS = 8;
axiosRetry(axios, { retries: 5, retryDelay: axiosRetry.exponentialDelay });

const tools = [{ type: "function" as const, function: { name: "lookup_order", parameters: { type: "object", properties: { id: { type: "string" } } } } }];

async function runTool(call: { function: { name: string; arguments: string } }): Promise<string> {
  const { id } = JSON.parse(call.function.arguments);
  return JSON.stringify((await axios.get(`http://orders/orders/${id}`)).data);
}

export async function answer(question: string): Promise<string> {
  const messages: any[] = [{ role: "user", content: question }];
  for (let turn = 0; turn < MAX_TURNS; turn++) {
    const reply = await client.chat.completions.create({ model: "gpt-4.1-mini", messages, tools });
    const message = reply.choices[0].message;
    messages.push(message);
    if (!message.tool_calls) return message.content ?? "";
    const results = await Promise.all(message.tool_calls.map((call) => runTool(call)));
    message.tool_calls.forEach((call, i) => messages.push({ role: "tool", tool_call_id: call.id, content: results[i] }));
  }
  return "";
}
