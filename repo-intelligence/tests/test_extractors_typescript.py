"""Unit tests for the TypeScript/JavaScript and Prisma extractors."""

from __future__ import annotations

import json

from repo_intel.extractors.prisma import PrismaSchema
from repo_intel.extractors.typescript_source import TypeScriptSource, next_route
from repo_intel.hashing import sha256_hex

TS = TypeScriptSource()
PRISMA = PrismaSchema()


def run(extractor, path: str, source: str):
    data = source.encode()
    return extractor.extract(path, data, sha256_hex(data)), data


def entities(result, kind: str) -> list[dict]:
    return [o.payload for o in result.observations if o.kind == "entity" and o.payload["kind"] == kind]


def of_kind(result, kind: str) -> list:
    return [o for o in result.observations if o.kind == kind]


def text(data: bytes, o) -> str:
    s = o.evidence.span
    return data[s.start_byte : s.end_byte].decode()


EXPRESS = """import express from "express";
const app = express();
const router = express.Router();
app.use(express.json());
app.get("/orders", async (_req, res) => { res.json([]); });
router.post("/orders", create);
app.delete("/orders/:id", auth, remove);
app.get("env");
app.get(prefix + "/dynamic", handler);
cache.get("key", fallback);
router.route("/items").get(list).put(replace);
app.listen(Number(process.env.PORT ?? 4000));
"""


def test_express_routes():
    result, data = run(TS, "apps/api/src/server.ts", EXPRESS)
    assert result.parsed
    endpoints = entities(result, "Endpoint")
    assert [e["name"] for e in endpoints][:3] == ["GET /orders", "POST /orders", "DELETE /orders/:id"]
    assert sorted(e["name"] for e in endpoints[3:]) == ["GET /items", "PUT /items"]  # router.route("/items").get().put()
    assert endpoints[0]["natural_key"] == {"component_root": "@unit", "protocol": "http", "method": "GET", "route": "/orders"}
    assert all(e["attributes"] == {"framework": "express"} for e in endpoints)
    handlers = of_kind(result, "route_handler")
    assert [(h.payload["method"], h.payload["route"], h.payload["handler"]) for h in handlers][:3] == [("GET", "/orders", None), ("POST", "/orders", "create"), ("DELETE", "/orders/:id", "remove")]


def test_express_evidence_span_is_tight():
    result, data = run(TS, "server.ts", EXPRESS)
    spans = [text(data, o) for o in result.observations if o.kind == "entity" and o.payload["kind"] == "Endpoint"]
    assert spans[:3] == ['app.get("/orders"', 'router.post("/orders"', 'app.delete("/orders/:id"']
    first = next(o for o in result.observations if o.kind == "entity" and o.payload["kind"] == "Endpoint")
    assert first.evidence.span.start_line == first.evidence.span.end_line == 5


def test_express_dynamic_path_yields_diagnostic_not_entity():
    result, _ = run(TS, "server.ts", EXPRESS)
    assert not any("dynamic" in e["natural_key"]["route"] for e in entities(result, "Endpoint"))
    infos = [d for d in result.diagnostics if d.severity == "info"]
    assert len(infos) == 1 and "not a string literal" in infos[0].message and "line 9" in infos[0].message


def test_express_commonjs_require():
    result, _ = run(TS, "server.js", 'const express = require("express");\nconst app = express();\napp.put("/a", h);\n')
    assert [e["name"] for e in entities(result, "Endpoint")] == ["PUT /a"]


def test_non_express_file_has_no_routes():
    result, _ = run(TS, "client.ts", 'import axios from "axios";\nconst app = make();\napp.get("/orders", cb);\naxios.post("/orders", body);\n')
    assert entities(result, "Endpoint") == []
    assert of_kind(result, "route_handler") == []


def test_next_route_path_derivation():
    assert next_route("apps/web/app/api/health/route.ts") == "/api/health"
    assert next_route("app/route.ts") == "/"
    assert next_route("src/app/x/route.js") == "/x"
    assert next_route("app/(marketing)/pricing/route.ts") == "/pricing"
    assert next_route("app/(group)/route.tsx") == "/"
    assert next_route("app/items/[id]/route.ts") == "/items/[id]"
    assert next_route("app/docs/[...slug]/route.ts") == "/docs/[...slug]"
    assert next_route("packages/app/src/app/api/v1/route.ts") == "/api/v1"
    assert next_route("src/api/route.ts") is None
    assert next_route("myapp/api/route.ts") is None
    assert next_route("app/api/page.tsx") is None
    assert next_route("route.ts") is None


def test_next_route_handlers():
    source = """import { helper } from "./helper";
export function GET() { return Response.json({ ok: true }); }
export const POST = async (req: Request) => Response.json({});
async function remove() {}
export { remove as DELETE };
export function helperFn() {}
function PUT() {}
export const dynamic = "force-dynamic";
"""
    result, data = run(TS, "apps/web/app/(shop)/items/[id]/route.ts", source)
    endpoints = entities(result, "Endpoint")
    assert sorted(e["name"] for e in endpoints) == ["DELETE /items/[id]", "GET /items/[id]", "POST /items/[id]"]
    assert endpoints[0]["natural_key"] == {"component_root": "@unit", "protocol": "http", "method": "GET", "route": "/items/[id]"}
    assert endpoints[0]["attributes"] == {"framework": "next"}
    spans = [text(data, o) for o in result.observations if o.kind == "entity" and o.payload["kind"] == "Endpoint"]
    assert spans == ["export function GET", "export const POST", "remove as DELETE"]


def test_next_exports_ignored_outside_app_router():
    result, _ = run(TS, "src/lib/route.ts", "export function GET() {}\n")
    assert entities(result, "Endpoint") == []


def test_process_env_names_without_values():
    source = """const port = process.env.PORT ?? 4000;
const key = process.env["API_KEY"] || "sk-live-secret-default";
const again = process.env.PORT;
const { DATABASE_URL, REDIS_URL: redis, MODE = "dev-secret" } = process.env;
const dyn = process.env[name];
"""
    result, data = run(TS, "src/config.ts", source)
    keys = entities(result, "ConfigKey")
    assert [k["natural_key"] for k in keys] == [{"name": "PORT"}, {"name": "API_KEY"}, {"name": "DATABASE_URL"}, {"name": "REDIS_URL"}, {"name": "MODE"}]
    rels = of_kind(result, "relation")
    assert len(rels) == 5
    assert all(r.payload["subject"] == "@package" and r.payload["predicate"] == "REFERENCES_CONFIG" and r.payload["object"]["kind"] == "ConfigKey" for r in rels)
    dumped = json.dumps([o.payload for o in result.observations])
    assert "secret" not in dumped and "4000" not in dumped
    config_obs = [o for o in result.observations if o.kind == "entity"]
    assert text(data, config_obs[0]) == "process.env.PORT"
    assert text(data, config_obs[1]) == 'process.env["API_KEY"]'


def test_imports_and_symbols():
    source = """import express, { Router } from "express";
import * as path from "node:path";
import type { Order } from "@acme/shared";
export * from "./types";
export { a as b } from "./a";
const fs = require("fs");
const lazy = () => import("./lazy");
export function run() {}
export class Service {}
export const LIMIT = 3, OTHER = 4;
export interface Shape { x: number }
export type Id = string;
export enum Colour { Red }
export default function Main() {}
function hidden() {}
"""
    result, data = run(TS, "src/index.ts", source)
    assert result.parsed
    imports = [(o.payload["specifier"], o.payload["names"]) for o in of_kind(result, "import")]
    assert imports == [("express", ["default", "Router"]), ("node:path", ["*"]), ("@acme/shared", ["Order"]), ("./types", ["*"]), ("./a", ["a"]), ("fs", []), ("./lazy", [])]
    symbols = [(o.payload["name"], o.payload["symbol_kind"]) for o in of_kind(result, "symbol")]
    assert symbols == [("run", "function"), ("Service", "class"), ("LIMIT", "const"), ("OTHER", "const"), ("Shape", "interface"), ("Id", "type"), ("Colour", "enum"), ("Main", "function")]
    assert text(data, of_kind(result, "symbol")[0]) == "export function run"


def test_fetch_http_calls():
    source = """const a = await fetch("/api/products");
const b = await fetch(`${BASE}/orders/${id}?token=${secret}`, { headers: { Authorization: "Bearer abc123" } });
const c = await fetch("https://user:hunter2@example.com/v1/items?api_key=abc123#frag");
const d = await fetch(url);
const e = await window.fetch(`/plain`);
client.fetch("/not-global");
"""
    result, data = run(TS, "src/api.ts", source)
    calls = of_kind(result, "http_call")
    assert [c.payload for c in calls] == [{"path": "/api/products"}, {"path": "{}/orders/{}"}, {"path": "https://example.com/v1/items"}, {"path": "/plain"}]
    assert text(data, calls[0]) == 'fetch("/api/products"'
    dumped = json.dumps([o.payload for o in result.observations])
    assert "abc123" not in dumped and "hunter2" not in dumped


def test_tsx_and_jsx_use_the_jsx_grammar():
    source = 'export function App() {\n  fetch("/api/products");\n  return <ul>{[1].map((p) => <li key={p}>{p}</li>)}</ul>;\n}\n'
    for path in ("src/App.tsx", "src/App.jsx", "src/App.js"):
        result, _ = run(TS, path, source)
        assert result.parsed, path
        assert [c.payload["path"] for c in of_kind(result, "http_call")] == ["/api/products"]
    generic, _ = run(TS, "src/id.ts", "export const id = <T>(x: T) => <T>x;\n")
    assert generic.parsed


def test_broken_typescript_does_not_raise():
    source = 'import express from "express";\nconst app = express();\napp.get("/ok", (req, res) => { res.json({});\nfunction ( {{{ \n'
    result, _ = run(TS, "server.ts", source)
    assert result.parsed is False
    assert any(d.severity == "warning" for d in result.diagnostics)
    for junk in (b"", b"\xff\xfe\x00\x01", b"}}}}((((", b"export"):
        TS.extract("x.ts", junk, sha256_hex(junk))
        TS.extract("app/route.ts", junk, sha256_hex(junk))


def test_matches():
    assert all(TS.matches(p) for p in ("a.ts", "a.tsx", "a.js", "a.jsx", "a.mjs", "a.cjs", "x/y/route.ts"))
    assert not any(TS.matches(p) for p in ("a.py", "a.json", "ts", "schema.prisma"))
    assert PRISMA.matches("apps/api/prisma/schema.prisma") and not PRISMA.matches("prisma/seed.ts")


SCHEMA = """// comment with model Fake {
datasource db {
  provider = "postgresql"
  url      = env("DATABASE_URL")
  shadowDatabaseUrl = env("SHADOW_URL") // trailing
}

generator client {
  provider = "prisma-client-js"
}

enum Status {
  OPEN
  CLOSED
}

model Order {
  id        Int      @id @default(autoincrement())
  sku       String
  createdAt DateTime @default(now()) @map("created_at")
  status    Status   @default(OPEN)
  tags      String[]
  note      String?  // optional
  price     Decimal  @db.Decimal(10, 2)
  customer  Customer @relation(fields: [customerId], references: [id])
  customerId Int     @map("customer_id")
  items     LineItem[]

  @@map("orders")
  @@index([sku])
}

model Customer {
  id     Int     @id
  orders Order[]
}

model LineItem {
  id Int @id
}
"""


def test_prisma_tables_and_fields():
    result, data = run(PRISMA, "prisma/schema.prisma", SCHEMA)
    assert result.parsed and not [d for d in result.diagnostics if d.severity != "info"]
    tables = entities(result, "Table")
    assert [t["natural_key"] for t in tables] == [{"datastore": "default", "name": "orders"}, {"datastore": "default", "name": "Customer"}, {"datastore": "default", "name": "LineItem"}]
    assert tables[0]["attributes"] == {"orm": "prisma", "model": "Order"}
    fields = entities(result, "Field")
    order_fields = [f["natural_key"]["name"] for f in fields if f["natural_key"]["table"] == "orders"]
    assert order_fields == ["id", "sku", "created_at", "status", "tags", "note", "price", "customer_id"]
    assert [f["natural_key"] for f in fields if f["natural_key"]["table"] == "Customer"] == [{"table": "Customer", "name": "id"}]
    contains = [o.payload for o in of_kind(result, "relation") if o.payload["predicate"] == "CONTAINS"]
    assert len(contains) == len(fields)
    assert contains[0]["subject"] == {"kind": "Table", "natural_key": {"datastore": "default", "name": "orders"}, "name": "orders"}
    assert contains[0]["object"]["kind"] == "Field" and contains[0]["object"]["natural_key"] == {"table": "orders", "name": "id"}


def test_prisma_config_keys_and_spans():
    result, data = run(PRISMA, "prisma/schema.prisma", SCHEMA)
    keys = [o for o in result.observations if o.kind == "entity" and o.payload["kind"] == "ConfigKey"]
    assert [k.payload["natural_key"]["name"] for k in keys] == ["DATABASE_URL", "SHADOW_URL"]
    assert text(data, keys[0]) == 'env("DATABASE_URL")'
    refs = [o.payload for o in of_kind(result, "relation") if o.payload["predicate"] == "REFERENCES_CONFIG"]
    assert [r["subject"] for r in refs] == ["@package", "@package"]
    table = next(o for o in result.observations if o.kind == "entity" and o.payload["kind"] == "Table")
    assert text(data, table) == "model Order"
    field = next(o for o in result.observations if o.kind == "entity" and o.payload["kind"] == "Field" and o.payload["natural_key"]["name"] == "created_at")
    assert text(data, field) == 'createdAt DateTime @default(now()) @map("created_at")'
    assert "postgresql" not in json.dumps([o.payload for o in result.observations])


def test_prisma_broken_schema_does_not_raise():
    result, _ = run(PRISMA, "schema.prisma", 'model Order {\n  id Int @id\n  ???\n\nmodel Other {\n  id Int\n')
    assert result.parsed is False
    assert any(d.severity == "warning" for d in result.diagnostics)
    assert [t["natural_key"]["name"] for t in entities(result, "Table")] == ["Order", "Other"]
    for junk in (b"", b"\xff\xfe\x00", b"}}}}", b"model {"):
        PRISMA.extract("schema.prisma", junk, sha256_hex(junk))


def test_kafka_topics_from_kafkajs_and_rdkafka():
    result, _ = run(TS, "svc/src/publisher.ts", """
import { Kafka } from "kafkajs";
import { Topics } from "./topics";
export const LOCAL = "orders.local";
const API_TOKEN = "abc123";
const producer = new Kafka({}).producer();
await producer.send({ topic: "orders.created", messages: [] });
await producer.send({ topic: Topics.SHIPPED, messages: [] });
await producer.send({ topic: LOCAL, messages: [] });
await producer.send({ topic: `orders.${region}`, messages: [] });
await consumer.subscribe({ topics: ["payments.settled", /audit.*/] });
mailer.send({ to: "a@b.c" });
""")
    uses = [(u.payload["role"], u.payload.get("topic"), u.payload.get("name"), u.payload.get("dynamic")) for u in of_kind(result, "topic_use")]
    assert uses == [
        ("publish", "orders.created", None, None), ("publish", None, "Topics.SHIPPED", None), ("publish", None, "LOCAL", None),
        ("publish", None, None, True), ("subscribe", "payments.settled", None, None), ("subscribe", None, None, True),
    ]
    assert {o.payload["name"]: o.payload["value"] for o in of_kind(result, "string_constant")} == {"LOCAL": "orders.local"}

    topics, _ = run(TS, "svc/src/topics.ts", 'export const Topics = { SHIPPED: "orders.shipped", DB_PASSWORD: "hunter2" } as const;\n')
    assert {o.payload["name"]: o.payload["value"] for o in of_kind(topics, "string_constant")} == {"Topics.SHIPPED": "orders.shipped"}

    rd, _ = run(TS, "svc/src/legacy.js", 'const Kafka = require("node-rdkafka");\nproducer.produce("orders.created", null, buf);\nconsumer.subscribe(["orders.created"]);\n')
    assert [(u.payload["role"], u.payload.get("topic")) for u in of_kind(rd, "topic_use")] == [("publish", "orders.created"), ("subscribe", "orders.created")]

    # No broker client imported: a `send` or `subscribe` call is not a topic use.
    plain, _ = run(TS, "svc/src/mail.ts", 'mailer.send({ topic: "hello" });\nstore.subscribe(listener);\n')
    assert not of_kind(plain, "topic_use")


# ---- GI-009: Prisma client call sites, unsafe raw SQL, test cases

PRISMA_USE = b'''import { PrismaClient } from "@prisma/client";
import { db } from "../lib/db";

const prisma = new PrismaClient();

export async function bulk(sql: string) {
  await prisma.$transaction(async (tx) => {
    await tx.orderItem.createMany({ data: [] });
  });
  await db.customer.findUnique({ where: { id: 1 } });
  await prisma.$queryRaw`SELECT 1`;
  return prisma.$executeRawUnsafe(sql);
}

class Repo {
  constructor(private prisma: PrismaClient) {}
  async all() {
    return this.prisma.order.findMany();
  }
}

const cache = new Map();
cache.order.findMany();
'''


def test_prisma_client_calls_become_orm_uses_and_unsafe_raw_sql_is_dynamic():
    from repo_intel.extractors.typescript_source import TypeScriptSource
    out = TypeScriptSource().extract("api/src/repo.ts", PRISMA_USE, "ab" * 32).observations
    uses = [(o.payload["function"], o.payload["function_kind"], o.payload["model"], o.payload["access"]) for o in out if o.kind == "orm_use"]
    assert uses == [("bulk", "function", "orderItem", "write"), ("bulk", "function", "customer", "read"), ("Repo.all", "method", "order", "read")]
    assert all(o.payload["orm"] == "prisma" for o in out if o.kind == "orm_use")
    assert [o.payload for o in out if o.kind == "dynamic_sql"] == [{"function": "bulk", "function_kind": "function"}], "tagged $queryRaw is parameterised, not dynamic"


def test_inline_express_callbacks_are_named_after_their_route():
    from repo_intel.extractors.typescript_source import TypeScriptSource
    src = b'import express from "express";\nimport { PrismaClient } from "@prisma/client";\nconst prisma = new PrismaClient();\nconst app = express();\napp.get("/orders", async (_req, res) => { res.json(await prisma.order.findMany()); });\nprisma.order.count();\n'
    out = TypeScriptSource().extract("api/src/server.ts", src, "ab" * 32).observations
    assert [(o.payload["function"], o.payload["function_kind"]) for o in out if o.kind == "orm_use"] == [("GET /orders", "route_handler"), ("<module>", "module")]


def test_typescript_test_cases_with_describe_chains_and_framework():
    from repo_intel.extractors.typescript_source import TypeScriptSource, is_test_file
    src = b'import { describe, it } from "vitest";\ndescribe("cart", () => {\n  it("adds", () => {});\n  describe("empty", () => { it.only("is zero", () => {}); });\n});\ntest("loose", () => {});\nit(name, () => {});\n'
    out = TypeScriptSource().extract("web/src/cart.test.ts", src, "ab" * 32).observations
    assert [o.payload for o in out if o.kind == "test"] == [{"name": "cart > adds", "framework": "vitest"}, {"name": "cart > empty > is zero", "framework": "vitest"}, {"name": "loose", "framework": "vitest"}]
    assert not [o for o in TypeScriptSource().extract("web/src/cart.ts", src, "ab" * 32).observations if o.kind == "test"], "only test files"
    assert is_test_file("a/b.spec.tsx") and is_test_file("a/__tests__/x.js") and not is_test_file("a/testing.ts")
