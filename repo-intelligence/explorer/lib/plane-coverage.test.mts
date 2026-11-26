// Run with `npm test` (node --test; Node strips the types).
import assert from "node:assert/strict";
import { test } from "node:test";
import { confirmedRatio, planeKindCounts, planeMeasures, securityRealms, statusKindsToFetch, statusShare, UI_PLATFORMS } from "./plane-coverage.ts";
import type { CoverageRow, Summary } from "./types.ts";

const experience = { id: "experience", kinds: ["Screen", "Form", "UIControl", "StyleToken", "Report"], coverage: ["accessibility"] };
const business = { id: "business", kinds: ["BusinessRule", "Workflow", "CodeList", "Requirement", "Feature"], coverage: ["transform_shapes"] };
const security = { id: "security", kinds: ["Role", "Permission"], coverage: [] };

function row(domain: string, scope: string, numerator: number, denominator: number): CoverageRow {
  return { domain, scope, numerator, denominator, detail: {} };
}

function summary(parts: Partial<Summary>): Summary {
  return { manifest: { commit_sha: "", repository_id: "", status: "complete", rkp_hash: "" }, topology: null, files: 0, counts_by_kind: {}, planes: {}, coverage_totals: {}, coverage: [], unknowns: [], conflicts: [], ...parts };
}

test("the experience plane shows the construct coverage of UI packs only, by pack name", () => {
  const s = summary({
    coverage: [row("platform_constructs", "web", 174, 216), row("platform_constructs", "catalog", 30, 33), row("platform_constructs", "cfml", 190, 192), row("parse", "web", 1, 1)],
    coverage_totals: { accessibility: { numerator: 3, denominator: 4 } },
  });
  assert.deepEqual(planeMeasures(s, experience), [
    { key: "accessibility", domain: "accessibility", ratio: { numerator: 3, denominator: 4 } },
    { key: "platform_constructs:cfml", domain: "platform_constructs", platform: "cfml", ratio: { numerator: 190, denominator: 192 } },
    { key: "platform_constructs:web", domain: "platform_constructs", platform: "web", ratio: { numerator: 174, denominator: 216 } },
  ]);
  assert.ok(!UI_PLATFORMS.includes("catalog"), "the catalogue pack has no UI terms");
});

test("a domain the snapshot did not measure is listed as not measured, not dropped", () => {
  // An engine without transform shapes (before wave 2) writes no transform_shapes coverage.
  assert.deepEqual(planeMeasures(summary({}), business), [{ key: "transform_shapes", domain: "transform_shapes", ratio: null }]);
  assert.deepEqual(planeMeasures(summary({ coverage_totals: { transform_shapes: { numerator: 5, denominator: 8 } } }), business)[0].ratio, { numerator: 5, denominator: 8 });
  assert.deepEqual(planeMeasures(summary({}), security), []);
});

test("kind counts come from the plane summary, falling back to counts by kind", () => {
  const s = summary({ planes: { business: { kinds: { BusinessRule: 4, Workflow: 0 }, total: 4 } }, counts_by_kind: { CodeList: 2 } });
  assert.deepEqual(planeKindCounts(s, business), [
    { kind: "BusinessRule", count: 4 }, { kind: "Workflow", count: 0 }, { kind: "CodeList", count: 2 }, { kind: "Requirement", count: 0 }, { kind: "Feature", count: 0 },
  ]);
  assert.deepEqual(statusKindsToFetch(s, business), ["BusinessRule"], "a kind with none found is not fetched");
  assert.deepEqual(statusKindsToFetch(summary({ counts_by_kind: { Role: 3 } }), security), []);
});

test("statuses are counted, and only entities with a status count towards the confirmed share", () => {
  const entities = [{ status: "confirmed" }, { status: "hypothesis" }, { status: "hypothesis" }, { status: "rejected" }, {}].map((attributes) => ({ attributes }));
  const share = statusShare("BusinessRule", 9, entities);
  assert.deepEqual(share, { kind: "BusinessRule", total: 9, counted: 5, confirmed: 1, hypothesis: 2, other: { rejected: 1 }, missing: 1 });
  assert.deepEqual(confirmedRatio(share), { numerator: 1, denominator: 4 });
});

test("with no status recorded there is no confirmed share", () => {
  // Rules from an engine that does not record status yet.
  const share = statusShare("BusinessRule", 2, [{ attributes: {} }, { attributes: { status: "" } }]);
  assert.equal(share.missing, 2);
  assert.equal(confirmedRatio(share), null);
  assert.equal(confirmedRatio(statusShare("Workflow", 0, [])), null);
});

test("roles and permissions are grouped by realm", () => {
  const e = (kind: string, realm?: string) => ({ kind, natural_key: realm === undefined ? {} : { realm, name: "x" } });
  assert.deepEqual(securityRealms([e("Role", "shopdb"), e("Permission", "shopdb"), e("Role", "cfml"), e("Role", "cfml"), e("Role"), e("Screen", "web")]), [
    { realm: "(no realm)", roles: 1, permissions: 0 },
    { realm: "cfml", roles: 2, permissions: 0 },
    { realm: "shopdb", roles: 1, permissions: 1 },
  ]);
});
