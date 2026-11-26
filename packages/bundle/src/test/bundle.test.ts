import { test } from "node:test";
import assert from "node:assert/strict";
import { promises as fs } from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { BundleError, loadBundle, readManifest } from "../index";
import type { BusinessRuleAttributes, CodeListAttributes, DataSchemaAttributes, DetailOmitted, RequirementAttributes, ScreenCapturedAttributes } from "../index";

const SPAN = { start_byte: 0, end_byte: 10, start_line: 3, end_line: 5 };

async function writeBundle(manifest: Record<string, unknown>, files: Record<string, unknown[]>): Promise<string> {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "rkp-bundle-"));
  const knowledge = path.join(root, ".rkp", "knowledge");
  await fs.mkdir(path.join(knowledge, "units"), { recursive: true });
  await fs.writeFile(path.join(knowledge, "manifest.json"), JSON.stringify(manifest));
  for (const [name, rows] of Object.entries(files)) {
    await fs.writeFile(path.join(knowledge, name), rows.map((r) => JSON.stringify(r)).join("\n") + "\n");
  }
  return root;
}

const manifest = {
  bundle_format: "rkp-bundle/1",
  engine: { name: "repo-intel", version: "0.1.0" },
  repository_id: "local/sample",
  commit_sha: "a".repeat(40),
  source_tree_hash: "sha256:x",
  toolchain_hash: "sha256:y",
  override_set_hash: "sha256:z",
  rkp_hash: "sha256:r",
  status: "complete",
  units: [{ unit: ".", file: "units/root.jsonl", entities: 2, assertions: 1, evidence: 1, unknowns: 1 }],
  counts: { entities: 2 },
};

const unitRows = [
  { table: "entity", id: "ent_b", kind: "Component", name: "worker", natural_key: {}, attributes: {}, evidence_ids: ["ev_1"] },
  { table: "entity", id: "ent_a", kind: "Component", name: "api", natural_key: {}, attributes: {}, evidence_ids: [] },
  { table: "assertion", id: "as_1", subject: "ent_a", predicate: "CALLS", object: "ent_b", qualifiers: {}, assertion_kind: "derived_relation", epistemic_status: "supported", evidence_class: "E3", evidence_ids: ["ev_1"], resolution: "exact", confidence: null, decision_ref: null },
  { table: "evidence", id: "ev_1", blob_sha256: "b1", span: SPAN, observation_kind: "relation", trust_label: "untrusted_data", sensitivity: "internal", secret_masked: false },
  { table: "unknown", id: "unk_1", subject: "ent_a", predicate: "component_kind", status: "unknown", reason: "ambiguous", coverage_ref: "c" },
];

test("loads and indexes a bundle", async () => {
  const root = await writeBundle(manifest, {
    "units/root.jsonl": unitRows,
    "files.jsonl": [{ path: "src/a.py", blob_sha256: "b1", size: 10, parsed: true }, { path: "copy/a.py", blob_sha256: "b1", size: 10, parsed: true }, { path: "logo.png", excluded: "binary" }],
    "unknowns.jsonl": [{ id: "unk_r", subject: "repo", predicate: "topology", status: "unknown", reason: "x", coverage_ref: "c" }],
  });
  const bundle = await loadBundle(root);
  assert.ok(bundle);
  assert.deepEqual(bundle.kinds(), ["Component"]);
  assert.deepEqual(bundle.ofKind("Component").map((e) => e.name), ["api", "worker"]);
  assert.equal(bundle.outgoing.get("ent_a")?.[0]?.object, "ent_b");
  assert.equal(bundle.incoming.get("ent_b")?.[0]?.subject, "ent_a");
  assert.deepEqual(bundle.locate("ev_1").map((l) => l.path).sort(), ["copy/a.py", "src/a.py"]);
  assert.equal(bundle.firstLocation(["missing", "ev_1"])?.startLine, 3);
  assert.equal(bundle.allUnknowns().length, 2);
  assert.equal(bundle.conflicts.length, 0);
  assert.equal(bundle.label("ent_b"), "worker (Component)");
});

test("no bundle is undefined, not an error", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "rkp-empty-"));
  assert.equal(await loadBundle(root), undefined);
});

test("reads formats 1 and 2, rejects others and unit files outside the bundle", async () => {
  assert.ok(await readManifest(await writeBundle({ ...manifest, bundle_format: "rkp-bundle/2" }, {})));
  const other = await writeBundle({ ...manifest, bundle_format: "rkp-bundle/9" }, {});
  await assert.rejects(readManifest(other), BundleError);
  const escaping = await writeBundle({ ...manifest, units: [{ ...manifest.units[0], file: "../../../etc/passwd" }] }, {});
  await assert.rejects(loadBundle(escaping), /outside the bundle/);
});

test("reports the line of invalid JSON", async () => {
  const root = await writeBundle(manifest, { "units/root.jsonl": [] });
  await fs.writeFile(path.join(root, ".rkp", "knowledge", "units", "root.jsonl"), "{}\nnot json\n");
  await assert.rejects(loadBundle(root), /root\.jsonl:2/);
});

test("fills in the fields format 2 leaves out, with separate copies per row", async () => {
  const rowDefaults = {
    entity: { attributes: {}, evidence_ids: [] },
    assertion: { assertion_kind: "derived_relation", epistemic_status: "supported", qualifiers: {}, resolution: "exact", confidence: null, decision_ref: null },
    evidence: { trust_label: "untrusted_data", sensitivity: "internal", secret_masked: false },
    unknown: { status: "unknown" },
  };
  const root = await writeBundle({ ...manifest, bundle_format: "rkp-bundle/2", row_defaults: rowDefaults }, {
    "units/root.jsonl": [
      { table: "entity", id: "ent_b", kind: "Component", name: "worker", natural_key: {}, evidence_ids: ["ev_1"] },
      { table: "entity", id: "ent_a", kind: "Component", name: "api", natural_key: {} },
      { table: "assertion", id: "as_1", subject: "ent_a", predicate: "CALLS", object: "ent_b", evidence_class: "E3", evidence_ids: ["ev_1"], confidence: 0.5 },
      { table: "evidence", id: "ev_1", blob_sha256: "b1", span: SPAN, observation_kind: "relation", secret_masked: true },
      { table: "unknown", id: "unk_1", subject: "ent_a", predicate: "component_kind", reason: "ambiguous", coverage_ref: "c" },
    ],
  });
  const bundle = await loadBundle(root);
  assert.ok(bundle);
  // Format 2 rows load to the same objects as the format 1 rows in unitRows, apart from the two non-default values.
  const full = (table: string, id: string) => {
    const { table: _, ...row } = unitRows.find((r) => r.table === table && r.id === id)!;
    return row;
  };
  assert.deepEqual(bundle.entities.get("ent_a"), full("entity", "ent_a"));
  assert.deepEqual(bundle.entities.get("ent_b"), full("entity", "ent_b"));
  assert.deepEqual(bundle.assertions.get("as_1"), { ...full("assertion", "as_1"), confidence: 0.5 });
  assert.deepEqual(bundle.evidence.get("ev_1"), { ...full("evidence", "ev_1"), secret_masked: true });
  assert.deepEqual(bundle.unknowns, [full("unknown", "unk_1")]);
  bundle.entities.get("ent_a")!.attributes.changed = true;
  assert.deepEqual(bundle.entities.get("ent_b")!.attributes, {});
});

test("reads exports.json when the manifest names it, and tolerates a bundle without it", async () => {
  const written = {
    operations: [{ interface: "ent_i", component: null, method: "GET", route: "/catalog", evidence_ids: ["ev_1"] }],
    unresolved_calls: [
      { caller: "ent_a", path: "/api/orders", method: "POST", evidence_ids: ["ev_1"] },
      { caller: "ent_a", path: "{}/health", evidence_ids: ["ev_1"] },
      { caller: "ent_b", path: "/api/stock", method: "GET", evidence_ids: ["ev_1"] },
    ],
  };
  const entry = { file: "exports.json", digest: "sha256:e", operations: 1, unresolved_calls: 3 };
  const root = await writeBundle({ ...manifest, bundle_format: "rkp-bundle/2", exports: entry }, { "units/root.jsonl": unitRows });
  await fs.writeFile(path.join(root, ".rkp", "knowledge", "exports.json"), JSON.stringify(written, null, 2));
  const bundle = await loadBundle(root);
  assert.ok(bundle);
  assert.deepEqual(bundle.exports, written);
  assert.deepEqual(bundle.unresolvedCallsOf("ent_a").map((c) => [c.method, c.path]), [["POST", "/api/orders"], [undefined, "{}/health"]]);
  assert.deepEqual(bundle.unresolvedCallsOf("ent_missing"), []);

  // A bundle written before exports.json existed: no manifest entry, no file.
  const old = await loadBundle(await writeBundle(manifest, { "units/root.jsonl": unitRows }));
  assert.ok(old);
  assert.equal(old.exports, undefined);
  assert.deepEqual(old.unresolvedCallsOf("ent_a"), []);
  assert.equal(old.entities.size, 2);

  // Named but missing still loads; a damaged or escaping file is an error.
  const missing = await loadBundle(await writeBundle({ ...manifest, exports: entry }, { "units/root.jsonl": unitRows }));
  assert.equal(missing?.exports, undefined);
  const damaged = await writeBundle({ ...manifest, exports: entry }, { "units/root.jsonl": unitRows });
  await fs.writeFile(path.join(damaged, ".rkp", "knowledge", "exports.json"), "{not json");
  await assert.rejects(loadBundle(damaged), /exports\.json is not valid JSON/);
  const wrongShape = await writeBundle({ ...manifest, exports: entry }, { "units/root.jsonl": unitRows });
  await fs.writeFile(path.join(wrongShape, ".rkp", "knowledge", "exports.json"), JSON.stringify({ operations: [] }));
  await assert.rejects(loadBundle(wrongShape), /operations and unresolved_calls/);
  const escaping = await writeBundle({ ...manifest, exports: { ...entry, file: "../../outside.json" } }, { "units/root.jsonl": unitRows });
  await assert.rejects(loadBundle(escaping), /outside the bundle/);
});

test("indexes behaviour rows by owner and skips tables it does not know", async () => {
  const behavior = (id: string, owner: string) => ({
    table: "behavior", id, owner, artifact: "ent_file", schema: "rkp-behavior/1", language: "cfml",
    params: [], body: [{ op: "exit", at: [0, 4] }], evidence_ids: ["ev_1"], evidence_class: "E2",
  });
  const root = await writeBundle({ ...manifest, bundle_format: "rkp-bundle/2", units: [{ ...manifest.units[0], behaviors: 2 }] }, {
    "units/root.jsonl": [...unitRows, { ...behavior("bhv_2", "ent_a"), kind: "data_step" }, behavior("bhv_1", "ent_a"), { table: "something_newer", id: "x" }],
  });
  const bundle = await loadBundle(root);
  assert.ok(bundle);
  assert.deepEqual(bundle.behaviorOf("ent_a").map((b) => b.id), ["bhv_1", "bhv_2"]);
  assert.equal(bundle.behaviorOf("ent_a")[0]?.language, "cfml");
  assert.deepEqual(bundle.behaviorOf("ent_a").map((b) => b.kind), [undefined, "data_step"], "a unit's kind is kept when it has one");
  assert.deepEqual(bundle.behaviorOf("ent_b"), []);
  assert.equal(bundle.entities.size, 2);
});

test("reads the architecture tier's omitted attributes and the attributes of rules, requirements, schemas and code lists", async () => {
  const entity = (id: string, kind: string, name: string, natural_key: Record<string, unknown>, attributes: Record<string, unknown>) =>
    ({ table: "entity", id, kind, name, natural_key, attributes, evidence_ids: [] });
  const rows = [
    ...unitRows,
    entity("ent_rule", "BusinessRule", "quantity-at-least-one", { scope: "approve.cfm", name: "quantity-at-least-one" },
      { statement: "An order needs a quantity of at least one.", rule_kind: "validation", status: "confirmed", candidates: [{ id: "c1", kind: "guard", path: "approve.cfm", at: [3, 40] }], confirmation_ref: "dec_1" }),
    entity("ent_req", "Requirement", "req-12", { scope: "docs/spec.md", name: "req-12" },
      { statement: "REQ-12 The desk shall keep a history of every order.", modality: "shall", identifier: "REQ-12", section: ["Orders", "History"] }),
    entity("ent_schema", "DataSchema", "docs/spec.md#orders", { name: "docs/spec.md#orders" }, { documents_table: "ent_table" }),
    entity("ent_codes", "CodeList", "docs/spec.md#status", { platform: "document", name: "docs/spec.md#status" }, { values: [{ code: "O", label: "Open" }, { code: "C", label: "Closed" }] }),
  ];
  const omitted = { entities: 4, assertions: 9, evidence: 3, unknowns: 0, behaviors: 2, transforms: 1, "Screen.captured": 1 };
  const root = await writeBundle({ ...manifest, tier: "architecture", detail_omitted: omitted, units: [{ ...manifest.units[0], entities: 6 }] }, { "units/root.jsonl": rows });
  const bundle = await loadBundle(root);
  assert.ok(bundle);
  const detail: DetailOmitted | undefined = bundle.manifest.detail_omitted;
  assert.equal(detail?.["Screen.captured"], 1);
  assert.equal(detail?.transforms, 1);
  const rule = bundle.ofKind("BusinessRule")[0]?.attributes as unknown as BusinessRuleAttributes;
  assert.equal(rule.status, "confirmed");
  assert.deepEqual(rule.candidates?.map((c) => c.at), [[3, 40]]);
  const req = bundle.ofKind("Requirement")[0]?.attributes as unknown as RequirementAttributes;
  assert.equal(req.identifier, "REQ-12");
  assert.equal((bundle.ofKind("DataSchema")[0]?.attributes as DataSchemaAttributes).documents_table, "ent_table");
  const codes = bundle.ofKind("CodeList")[0]?.attributes as CodeListAttributes;
  assert.deepEqual(codes.values?.map((v) => v.code), ["O", "C"]);
  const screen: ScreenCapturedAttributes = {};
  assert.equal(screen.captured, undefined);
});
