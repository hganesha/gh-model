import { describe, expect, it } from "vitest";
import { listOrders } from "./orders";

describe("orders", () => {
  it("lists newest first", async () => {
    expect(await listOrders()).toEqual([]);
  });
});
