// Run with `npm test` (node --test; Node strips the types).
import assert from "node:assert/strict";
import { test } from "node:test";
import { countByDocument, designation, documentOf, effectiveFeatures, matches, otherEntries, problemText, producingPaths, sameEntries, saveBody, saveFailureText, STALE_DESIGNATIONS, toggle, unreadableDocuments } from "./documents.ts";
import type { DocumentEntry } from "./documents.ts";
import type { CoverageRow } from "./types.ts";

const entries: DocumentEntry[] = [
  { path: "docs/pricing-rules.md", features: ["rules"], note: "signed off by finance" },
  { path: "specs/**/*.docx", features: ["requirements", "schema"] },
];

test("globs match as the designation file defines them", () => {
  assert.ok(matches("specs/**/*.docx", "specs/a.docx"));
  assert.ok(matches("specs/**/*.docx", "specs/x/y/a.docx"));
  assert.ok(!matches("specs/*.docx", "specs/x/a.docx"));
  assert.ok(!matches("docs/a.md", "docs/aamd"), "a dot is literal");
});

test("a candidate shows its own features and those a glob gives it", () => {
  assert.deepEqual(designation(entries, "docs/pricing-rules.md"), { own: ["rules"], inherited: [] });
  assert.deepEqual(designation(entries, "specs/v2/orders.docx"), { own: [], inherited: [{ path: "specs/**/*.docx", features: ["requirements", "schema"] }] });
  assert.deepEqual(effectiveFeatures([...entries, { path: "specs/v2/orders.docx", features: ["code_lists"] }], "specs/v2/orders.docx"), ["requirements", "schema", "code_lists"]);
});

test("toggling edits only the file's own entry, keeps its note, and removes it when empty", () => {
  const on = toggle(entries, "docs/pricing-rules.md", "code_lists", true);
  assert.deepEqual(on[0], { path: "docs/pricing-rules.md", features: ["rules", "code_lists"], note: "signed off by finance" });
  assert.deepEqual(toggle(entries, "docs/pricing-rules.md", "rules", false), [entries[1]]);
  const added = toggle(entries, "a/readme.md", "requirements", true);
  assert.deepEqual(added.map((e) => e.path), ["a/readme.md", "docs/pricing-rules.md", "specs/**/*.docx"], "kept sorted by path");
  assert.deepEqual(toggle(entries, "specs/v2/orders.docx", "schema", false), entries, "a glob's features are not removed from one file");
  assert.deepEqual(toggle([{ path: "x.md", features: ["rules", "summaries"] }], "x.md", "rules", false), [{ path: "x.md", features: ["summaries"] }], "an unknown feature survives a save");
});

test("entries the candidate list cannot show are listed apart", () => {
  assert.deepEqual(otherEntries(entries, [{ path: "docs/pricing-rules.md", language: "markdown" }]), [entries[1]]);
  assert.ok(sameEntries(entries, [...entries].reverse()));
  assert.ok(!sameEntries(entries, toggle(entries, "docs/pricing-rules.md", "schema", true)));
});

test("produced entities are counted under the document their natural key names", () => {
  const paths = new Set(["docs/pricing-rules.md", "docs/a#b.md"]);
  const produced = [
    { kind: "Requirement", natural_key: { scope: "docs/pricing-rules.md", name: "req-1" } },
    { kind: "Requirement", natural_key: { scope: "docs/pricing-rules.md", name: "req-2" } },
    { kind: "BusinessRule", natural_key: { scope: "docs/pricing-rules.md", name: "discount-cap" } },
    { kind: "BusinessRule", natural_key: { scope: "billing", name: "from-code" } },
    { kind: "DataSchema", natural_key: { name: "docs/a#b.md#Order" } },
    { kind: "CodeList", natural_key: { platform: "document", name: "docs/pricing-rules.md#tiers" } },
    { kind: "CodeList", natural_key: { platform: "cobol", name: "docs/pricing-rules.md#tiers" } },
  ];
  assert.equal(documentOf(produced[3]!, paths), undefined, "a rule from code is not a document's");
  assert.deepEqual(countByDocument(produced, paths), {
    "docs/pricing-rules.md": { Requirement: 2, BusinessRule: 1, CodeList: 1 },
    "docs/a#b.md": { DataSchema: 1 },
  });
  assert.deepEqual(producingPaths(produced.slice(0, 3)), ["docs/pricing-rules.md"]);
});

test("unreadable documents and problems read from the shapes the engine may write", () => {
  const rows: CoverageRow[] = [
    { domain: "documents", scope: "unit", numerator: 3, denominator: 4, detail: { unreadable: [{ path: "b.pdf", needs: "Markdown, DOCX or text" }, "a.pdf"] } },
    { domain: "parse", scope: "unit", numerator: 1, denominator: 1, detail: { unreadable: ["x.pdf"] } },
  ];
  assert.deepEqual(unreadableDocuments(rows), [{ path: "a.pdf", needs: null }, { path: "b.pdf", needs: "Markdown, DOCX or text" }]);
  assert.equal(problemText({ path: "x/*.md", message: "matches no file" }), "x/*.md: matches no file");
  assert.equal(problemText("unknown feature"), "unknown feature");
});

test("a save sends the version the entries were edited from, and a 409 asks for a reload", () => {
  assert.deepEqual(saveBody(entries, { version: 4096 }), { documents: entries, expected_version: 4096 });
  assert.deepEqual(saveBody([], { version: 0 }), { documents: [], expected_version: 0 }, "0 is the version of a missing file, and is still checked");
  assert.deepEqual(saveBody(entries, {}), { documents: entries }, "an engine without versions gets no expected_version");
  assert.equal(saveFailureText(409, "The designations changed since they were read; reload and try again."), STALE_DESIGNATIONS);
  assert.equal(STALE_DESIGNATIONS, "These designations changed since you opened them. Reload and try again.");
  assert.equal(saveFailureText(422, "unknown feature: summaries"), "unknown feature: summaries");
  assert.equal(saveFailureText(undefined, undefined), "The designations could not be saved.");
});
