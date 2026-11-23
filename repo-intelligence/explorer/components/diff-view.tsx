"use client";

import { ArrowRight } from "lucide-react";
import { useMemo, useState, type ReactNode } from "react";
import { useApi } from "@/lib/api";
import { displayValue, kindLabel, plural, shortSha } from "@/lib/format";
import type { OrderBasis } from "@/lib/snapshots";
import { PLANE_BY_ID, PLANES, planeOfKind } from "@/lib/planes";
import type { AssertionCounts, ChangeCounts, DiffAssertion, DiffEntity, DiffResponse, EntityRef, ImpactRow, SnapshotRef, Support } from "@/lib/types";
import { Badge, ErrorNote, Loading } from "./ui";

type Open = (id: string, hash: string) => void;

const CAUSE_TEXT: Record<string, string> = {
  none: "These two snapshots describe the same knowledge.",
  source_change: "Only the code changed between these two compiles; the compiler was the same.",
  toolchain_change: "The code is the same in both; the compiler changed, so every difference comes from the compiler.",
  undetermined: "Both the code and the compiler changed, so a difference cannot be attributed to either one.",
};

function planeTitle(plane: string | null): string {
  if (plane === "files") return "Files";
  if (plane === null) return "Other";
  return PLANE_BY_ID[plane]?.title ?? plane;
}

function n(value: number | undefined): string {
  return (value ?? 0).toLocaleString("en-US");
}

/** Renders the first `limit` rows and a button for the rest, so long lists stay readable. */
function Capped<T>({ items, limit, noun, render }: { items: T[]; limit: number; noun: string; render: (item: T, index: number) => ReactNode }) {
  const [all, setAll] = useState(false);
  const shown = all ? items : items.slice(0, limit);
  return (
    <>
      {shown.map(render)}
      {!all && items.length > limit && (
        <li className="capped-more">
          <button type="button" className="link-btn" onClick={() => setAll(true)}>
            Show all {items.length.toLocaleString("en-US")} {noun}
          </button>
        </li>
      )}
    </>
  );
}

function EntityButton({ entity, hash, onOpen }: { entity: EntityRef; hash: string; onOpen: Open }) {
  return (
    <button type="button" className="diff-entity" onClick={() => onOpen(entity.id, hash)} title={kindLabel(entity.kind)}>
      <i className={`dot plane-${planeOfKind(entity.kind)}`} aria-hidden="true" />
      <span>{entity.name}</span>
    </button>
  );
}

function Counts({ c }: { c: ChangeCounts }) {
  const cell = (value: number, sign: string, cls: string, label: string) => (
    <span className={`delta ${cls} ${value ? "" : "is-zero"}`} title={`${value} ${label}`}>
      <span aria-hidden="true">
        {sign}
        {n(value)}
      </span>
      <span className="sr-only">
        {value} {label}
      </span>
    </span>
  );
  return (
    <span className="deltas">
      {cell(c.added, "+", "delta-add", "added")}
      {cell(c.removed, "−", "delta-del", "removed")}
      {cell(c.changed, "~", "delta-chg", "changed")}
      {cell(c.moved, "→", "delta-mov", "moved")}
    </span>
  );
}

function PathList({ label, paths }: { label: string; paths: string[] }) {
  if (!paths.length) return null;
  return (
    <details className="expander">
      <summary>
        <span>{label}</span>
        <span className="expander-meta">
          <code>{paths.length}</code>
        </span>
      </summary>
      <ul className="mono-list">
        {paths.map((p) => (
          <li key={p}>{p}</li>
        ))}
      </ul>
    </details>
  );
}

function SupportBadges({ support }: { support: Support | undefined }) {
  if (!support) return <span className="muted">not present</span>;
  const status = support.epistemic_status ?? "unknown";
  return (
    <span className="badges">
      <Badge tone={status === "verified" ? "verified" : status === "supported" ? "supported" : "inferred"}>{status}</Badge>
      {support.evidence_class && <Badge tone="class">{support.evidence_class}</Badge>}
      {support.resolution === "candidate" && <Badge tone="candidate">candidate</Badge>}
    </span>
  );
}

// ---------- entities ----------

const ENTITY_SECTIONS: { change: string; title: string; hint: string }[] = [
  { change: "added", title: "Added", hint: "Exists in the newer snapshot only." },
  { change: "removed", title: "Removed", hint: "Exists in the older snapshot only. Rows open in the older snapshot." },
  { change: "changed", title: "Changed", hint: "Same thing in both snapshots, with different recorded facts." },
  { change: "moved", title: "Moved", hint: "The same thing at a new location, established from identical content rather than from its name." },
];

function EntityRow({ entity, hash, onOpen }: { entity: DiffEntity; hash: string; onOpen: Open }) {
  const fields = (entity.changed_fields ?? []).filter((f) => entity.change !== "evidence_changed" || f !== "evidence");
  return (
    <li className="diff-row">
      <EntityButton entity={entity} hash={hash} onOpen={onOpen} />
      <span className="diff-kind">{kindLabel(entity.kind)}</span>
      {entity.label && (
        <span className="diff-label" title={entity.label}>
          {entity.label}
        </span>
      )}
      {entity.change === "moved" && (
        <span className="diff-move">
          <code>{displayValue(entity.from_key)}</code>
          <ArrowRight size={12} aria-label="moved to" />
          <code>{displayValue(entity.to_key)}</code>
          {entity.basis !== undefined && entity.basis !== null && <span className="muted">matched by {displayValue(entity.basis)}</span>}
        </span>
      )}
      {fields.length > 0 && (
        <span className="diff-fields">
          {fields.map((f) => (
            <code key={f}>{f}</code>
          ))}
        </span>
      )}
    </li>
  );
}

function Entities({ diff, fromHash, toHash, onOpen }: { diff: DiffResponse; fromHash: string; toHash: string; onOpen: Open }) {
  const [plane, setPlane] = useState<string | null>(null);
  const [kind, setKind] = useState<string | null>(null);

  const planes = useMemo(() => {
    const counts = new Map<string, number>();
    for (const e of diff.entities) counts.set(e.plane ?? "other", (counts.get(e.plane ?? "other") ?? 0) + 1);
    const order = [...PLANES.map((p) => p.id as string), "files", "other"];
    return order.filter((p) => counts.has(p)).map((p) => ({ id: p, count: counts.get(p)! }));
  }, [diff]);

  const inPlane = useMemo(() => diff.entities.filter((e) => plane === null || (e.plane ?? "other") === plane), [diff, plane]);
  const kinds = useMemo(() => {
    const counts = new Map<string, number>();
    for (const e of inPlane) counts.set(e.kind, (counts.get(e.kind) ?? 0) + 1);
    return [...counts.entries()].sort((a, b) => a[0].localeCompare(b[0]));
  }, [inPlane]);
  const activeKind = kind !== null && kinds.some(([k]) => k === kind) ? kind : null;
  const visible = inPlane.filter((e) => activeKind === null || e.kind === activeKind);
  const of = (change: string) => visible.filter((e) => e.change === change);
  const edited = of("evidence_changed");
  const filtered = plane !== null || activeKind !== null;

  return (
    <section className="panel">
      <h3>
        Entities <span className="section-count">{diff.entities.length.toLocaleString("en-US")} touched</span>
      </h3>
      <div className="filters diff-filters">
        <div className="chips" role="group" aria-label="Filter by plane">
          <button type="button" className={`chip ${plane === null ? "is-on" : ""}`} aria-pressed={plane === null} onClick={() => setPlane(null)}>
            All planes <i>{diff.entities.length}</i>
          </button>
          {planes.map((p) => (
            <button type="button" key={p.id} className={`chip ${plane === p.id ? "is-on" : ""}`} aria-pressed={plane === p.id} onClick={() => setPlane(p.id)}>
              {planeTitle(p.id === "other" ? null : p.id)} <i>{p.count}</i>
            </button>
          ))}
        </div>
        {kinds.length > 1 && (
          <div className="chips" role="group" aria-label="Filter by kind">
            <button type="button" className={`chip chip-light ${activeKind === null ? "is-on" : ""}`} aria-pressed={activeKind === null} onClick={() => setKind(null)}>
              All kinds
            </button>
            {kinds.map(([k, count]) => (
              <button type="button" key={k} className={`chip chip-light ${activeKind === k ? "is-on" : ""}`} aria-pressed={activeKind === k} onClick={() => setKind(k)}>
                {kindLabel(k)} <i>{count}</i>
              </button>
            ))}
          </div>
        )}
      </div>

      {ENTITY_SECTIONS.map((section) => {
        const rows = of(section.change);
        return (
          <div className={`diff-section diff-${section.change}`} key={section.change}>
            <h4>
              {section.title} <span className="section-count">{rows.length}</span>
            </h4>
            {rows.length === 0 ? (
              <p className="group-note">{filtered ? "Nothing in this category matches the filter." : "Nothing in this category."}</p>
            ) : (
              <>
                <p className="group-note">{section.hint}</p>
                <ul className="diff-list">
                  <Capped items={rows} limit={40} noun="rows" render={(e) => <EntityRow key={`${e.change}:${e.id}`} entity={e} hash={section.change === "removed" ? fromHash : toHash} onOpen={onOpen} />} />
                </ul>
              </>
            )}
          </div>
        );
      })}

      <details className="expander diff-edited">
        <summary>
          <span>In edited files</span>
          <span className="expander-meta">
            <Badge tone="unknown">{plural(edited.length, "entity", "entities")}</Badge>
          </span>
        </summary>
        <p className="group-note">
          The file that defines each of these was edited. Whether the thing itself changed is not known at that grain, so treat this list as “possibly affected”, not as “changed”.
        </p>
        {edited.length === 0 ? (
          <p className="group-note">{filtered ? "Nothing here matches the filter." : "Nothing in this category."}</p>
        ) : (
          <ul className="diff-list">
            <Capped items={edited} limit={60} noun="rows" render={(e) => <EntityRow key={e.id} entity={e} hash={toHash} onOpen={onOpen} />} />
          </ul>
        )}
      </details>
    </section>
  );
}

// ---------- relationships ----------

const ASSERTION_SECTIONS: { change: string; title: string }[] = [
  { change: "added", title: "Added" },
  { change: "removed", title: "Removed" },
  { change: "support_changed", title: "Support changed" },
];

function AssertionRow({ a, fromHash, toHash, onOpen }: { a: DiffAssertion; fromHash: string; toHash: string; onOpen: Open }) {
  const hash = a.change === "removed" ? fromHash : toHash;
  const weak = (a.after ?? a.before)?.resolution === "candidate" || (a.after ?? a.before)?.epistemic_status === "inferred";
  return (
    <li className={`diff-row relation-row ${weak ? "is-tentative" : ""}`}>
      <span className="relation-triple">
        <EntityButton entity={a.subject} hash={hash} onOpen={onOpen} />
        <span className="relation-arrow">
          — <span className="predicate">{a.predicate}</span> <ArrowRight size={12} aria-hidden="true" />
        </span>
        <EntityButton entity={a.object} hash={hash} onOpen={onOpen} />
      </span>
      <span className="relation-support">
        {a.change === "support_changed" ? (
          <>
            <SupportBadges support={a.before} />
            <ArrowRight size={12} aria-label="became" />
            <SupportBadges support={a.after} />
          </>
        ) : (
          <SupportBadges support={a.change === "removed" ? a.before : a.after} />
        )}
      </span>
    </li>
  );
}

function Relationships({ diff, fromHash, toHash, onOpen }: { diff: DiffResponse; fromHash: string; toHash: string; onOpen: Open }) {
  const [structural, setStructural] = useState(false);
  const s: AssertionCounts = diff.summary.structural_assertions;
  const structuralTotal = s.added + s.removed + s.support_changed;
  const rows = diff.assertions.filter((a) => structural || !a.structural);
  return (
    <section className="panel">
      <h3>
        Relationships <span className="section-count">{rows.length.toLocaleString("en-US")} shown</span>
      </h3>
      <label className="toggle">
        <input type="checkbox" checked={structural} onChange={(e) => setStructural(e.target.checked)} />
        <span>
          Include structural relationships (CONTAINS, DEFINED_IN) <i>{structuralTotal}</i>
        </span>
      </label>
      {ASSERTION_SECTIONS.map((section) => {
        const list = rows.filter((a) => a.change === section.change);
        return (
          <div className={`diff-section diff-${section.change}`} key={section.change}>
            <h4>
              {section.title} <span className="section-count">{list.length}</span>
            </h4>
            {list.length === 0 ? (
              <p className="group-note">Nothing in this category.</p>
            ) : (
              <ul className="diff-list">
                <Capped items={list} limit={30} noun="relationships" render={(a) => <AssertionRow key={`${a.change}:${a.id}`} a={a} fromHash={fromHash} toHash={toHash} onOpen={onOpen} />} />
              </ul>
            )}
          </div>
        );
      })}
    </section>
  );
}

// ---------- impact ----------

const WHY: Record<string, string> = {
  added: "was added",
  changed: "changed",
  moved: "moved",
  evidence_changed: "is in an edited file",
  relationship_added: "this relationship is new",
};

const IMPACT_PLANE_ORDER = ["tests", "contracts", "architecture", "data", "ai", "deployment", "files", "other"];

function Impact({ diff, toHash, onOpen }: { diff: DiffResponse; toHash: string; onOpen: Open }) {
  const [broad, setBroad] = useState(false);
  const broadCount = diff.impact.filter((r) => r.because_change === "evidence_changed").length;

  const groups = useMemo(() => {
    const rows = diff.impact.filter((r) => broad || r.because_change !== "evidence_changed");
    // plane -> kind -> impacted entity -> reasons
    const byPlane = new Map<string, Map<string, Map<string, { entity: EntityRef; reasons: ImpactRow[] }>>>();
    for (const r of rows) {
      const plane = r.plane ?? "other";
      if (!byPlane.has(plane)) byPlane.set(plane, new Map());
      const byKind = byPlane.get(plane)!;
      if (!byKind.has(r.entity.kind)) byKind.set(r.entity.kind, new Map());
      const byEntity = byKind.get(r.entity.kind)!;
      if (!byEntity.has(r.entity.id)) byEntity.set(r.entity.id, { entity: r.entity, reasons: [] });
      byEntity.get(r.entity.id)!.reasons.push(r);
    }
    return [...byPlane.entries()].map(([plane, byKind]) => ({
        plane,
        kinds: [...byKind.entries()].sort((a, b) => a[0].localeCompare(b[0])).map(([kind, byEntity]) => ({ kind, entities: [...byEntity.values()] })),
      }));
  }, [diff, broad]);

  // Hidden broad rows per plane, so an empty "Tests" group never reads as "no tests affected".
  const hiddenByPlane = useMemo(() => {
    const m = new Map<string, number>();
    if (!broad) for (const r of diff.impact) if (r.because_change === "evidence_changed") m.set(r.plane ?? "other", (m.get(r.plane ?? "other") ?? 0) + 1);
    return m;
  }, [diff, broad]);
  // One ordered list of planes (tests first), whether a plane has visible rows or only hidden ones.
  const rank = (p: string) => (IMPACT_PLANE_ORDER.indexOf(p) === -1 ? IMPACT_PLANE_ORDER.length : IMPACT_PLANE_ORDER.indexOf(p));
  const sections = [...new Set([...groups.map((g) => g.plane), ...hiddenByPlane.keys()])]
    .sort((x, y) => rank(x) - rank(y))
    .map((plane) => ({ plane, group: groups.find((g) => g.plane === plane) ?? null, hidden: hiddenByPlane.get(plane) ?? 0 }));

  return (
    <section className="panel">
      <h3>
        Impact <span className="section-count">{diff.impact.length.toLocaleString("en-US")} rows</span>
      </h3>
      <p className="group-note">Things one relationship away from something that changed, as recorded in the newer snapshot. It is a reading list, not a guarantee that they broke.</p>
      <label className="toggle">
        <input type="checkbox" checked={broad} onChange={(e) => setBroad(e.target.checked)} />
        <span>
          Include things only in edited files <i>{broadCount}</i>
        </span>
      </label>
      {diff.impact.length === 0 && <p className="group-note">Nothing was recorded as impacted.</p>}
      {sections.map(({ plane, group, hidden }) => (
        <div className={`diff-section impact-plane plane-${plane}`} key={plane}>
          <h4>
            <i className={`dot plane-${plane}`} aria-hidden="true" />
            {plane === "tests" ? "Tests to consider running" : planeTitle(plane === "other" ? null : plane)}
            {group && hidden > 0 ? <span className="section-count">{hidden} more only in edited files</span> : null}
          </h4>
          {!group && (
            <p className="group-note">
              Nothing here is tied to a specific change. {plural(hidden, "row")} {hidden === 1 ? "is" : "are"} hidden because {hidden === 1 ? "it is" : "they are"} only connected to edited files —{" "}
              <button type="button" className="link-btn" onClick={() => setBroad(true)}>
                include them
              </button>
              .
            </p>
          )}
          {group?.kinds.map((k) => (
            <div className="impact-kind" key={k.kind}>
              <h5>
                {kindLabel(k.kind)} <span className="section-count">{k.entities.length}</span>
              </h5>
              <ul className="diff-list">
                <Capped
                  items={k.entities}
                  limit={25}
                  noun={`${kindLabel(k.kind).toLowerCase()} entities`}
                  render={(item) => (
                    <li className="impact-entity" key={item.entity.id}>
                      <EntityButton entity={item.entity} hash={toHash} onOpen={onOpen} />
                      <ul className="impact-reasons">
                        <Capped
                          items={item.reasons}
                          limit={6}
                          noun="reasons"
                          render={(r, i) => (
                            <li key={i} className={r.certainty === "candidate" ? "is-candidate" : ""}>
                              <span className="muted">— via</span> <span className="predicate">{r.via}</span> <span className="muted">— because</span>{" "}
                              <EntityButton entity={r.because} hash={toHash} onOpen={onOpen} /> <span className="impact-why">({WHY[r.because_change] ?? r.because_change})</span>
                              {r.certainty === "candidate" && <Badge tone="candidate">candidate</Badge>}
                            </li>
                          )}
                        />
                      </ul>
                    </li>
                  )}
                />
              </ul>
            </div>
          ))}
        </div>
      ))}
    </section>
  );
}

// ---------- summary ----------

function KindSummary({ diff }: { diff: DiffResponse }) {
  const byPlane = useMemo(() => {
    const order = [...PLANES.map((p) => p.id as string), "files"];
    const groups = new Map<string, [string, ChangeCounts][]>();
    for (const [kind, counts] of Object.entries(diff.summary.by_kind ?? {})) {
      const plane = planeOfKind(kind);
      if (!groups.has(plane)) groups.set(plane, []);
      groups.get(plane)!.push([kind, counts]);
    }
    return order.filter((p) => groups.has(p)).map((p) => ({ plane: p, kinds: groups.get(p)!.sort((a, b) => a[0].localeCompare(b[0])) }));
  }, [diff]);
  const shards = diff.summary.shards;

  return (
    <section className="panel">
      <h3>By plane and kind</h3>
      <p className="group-note delta-key">
        <span className="delta delta-add">+ added</span> <span className="delta delta-del">− removed</span> <span className="delta delta-chg">~ changed</span>{" "}
        <span className="delta delta-mov">→ moved</span> <span className="muted">· “edited file” counts are possibly affected, not known changes</span>
      </p>
      {byPlane.length === 0 && <p className="group-note">No entity kinds differ.</p>}
      <div className="kind-summary">
        {byPlane.map((g) => (
          <div className={`kind-summary-plane plane-${g.plane}`} key={g.plane}>
            <h4>
              <i className={`dot plane-${g.plane}`} aria-hidden="true" />
              {planeTitle(g.plane)}
            </h4>
            <ul>
              {g.kinds.map(([kind, c]) => (
                <li key={kind}>
                  <span className="kind-name">{kindLabel(kind)}</span>
                  <Counts c={c} />
                  <span className={`edited-count ${c.evidence_changed ? "" : "is-zero"}`}>{n(c.evidence_changed)} in edited files</span>
                </li>
              ))}
            </ul>
          </div>
        ))}
      </div>
      {shards && (
        <p className="group-note">
          Compile units: {shards.changed?.length ?? 0} changed
          {shards.changed?.length ? ` (${shards.changed.join(", ")})` : ""}, {shards.added?.length ?? 0} added
          {shards.added?.length ? ` (${shards.added.join(", ")})` : ""}, {shards.removed?.length ?? 0} removed
          {shards.removed?.length ? ` (${shards.removed.join(", ")})` : ""}, {n(shards.unchanged)} unchanged.
        </p>
      )}
    </section>
  );
}

export function DiffView({ from, to, sameCommit, basis, onOpenEntity }: { from: SnapshotRef; to: SnapshotRef; sameCommit: boolean; basis: OrderBasis; onOpenEntity: Open }) {
  const query = `knowledge/diff?${new URLSearchParams({ from: from.rkp_hash, to: to.rkp_hash })}`;
  const { data, error, loading, reload } = useApi<DiffResponse>(query);

  const header = (
    <p className="diff-pair">
      <span>
        <span className="eyebrow">FROM</span> <code>{shortSha(from.commit_sha)}</code> {from.ref}
      </span>
      <ArrowRight size={14} aria-label="to" />
      <span>
        <span className="eyebrow">TO</span> <code>{shortSha(to.commit_sha)}</code> {to.ref}
      </span>
    </p>
  );

  if (loading) {
    return (
      <div className="diff-view">
        {header}
        <Loading label="Comparing snapshots" />
      </div>
    );
  }
  if (error) {
    return (
      <div className="diff-view">
        {header}
        <ErrorNote error={error} onRetry={reload} what="the comparison" />
      </div>
    );
  }
  if (!data) return null;

  const e = data.summary.entities;
  const files = data.files;
  const nothing = data.cause === "none";

  return (
    <div className="diff-view">
      <header className="lead">
        {header}
        <h2>
          {nothing
            ? "These two snapshots describe the same knowledge."
            : `${n(e.added)} added, ${n(e.removed)} removed, ${n(e.changed)} changed, ${n(e.moved)} moved; ${n(e.evidence_changed)} more are in files that were edited.`}
        </h2>
        {!nothing && <p>{CAUSE_TEXT[data.cause] ?? `Cause: ${data.cause}`}</p>}
        <p className="group-note">
          {basis === "commit"
            ? "“From” is the older commit, by commit date."
            : sameCommit
              ? "Both were compiled from the same commit; “from” is the earlier compile."
              : "A commit date is missing for one of these, so “from” is the older compile, not necessarily the older commit."}{" "}
          {n(data.summary.impacted)} things are one relationship away from
          a change.
        </p>
        {data.truncated && <p className="notice notice-warn">Truncated: the server returned only part of this comparison. Counts in the summary are complete; the lists below are not.</p>}
      </header>

      {nothing && e.added + e.removed + e.changed + e.moved + e.evidence_changed === 0 && data.assertions.length === 0 ? (
        <div className="empty-plane">
          <h3>Nothing differs.</h3>
          <p>Same code, same compiler, same result: every entity and relationship is identical in both snapshots.</p>
        </div>
      ) : (
        <>
          <section className="panel">
            <h3>Files</h3>
            {!files?.available ? (
              <p className="group-note">File-level changes are not available for this pair of snapshots. That is unknown, not “no files changed”.</p>
            ) : (
              <>
                <p className="files-line">
                  <span className="delta delta-add">+{n(files.added.length)} added</span> <span className="delta delta-del">−{n(files.removed.length)} removed</span>{" "}
                  <span className="delta delta-chg">~{n(files.modified.length)} modified</span>
                </p>
                <PathList label="Added files" paths={files.added} />
                <PathList label="Removed files" paths={files.removed} />
                <PathList label="Modified files" paths={files.modified} />
              </>
            )}
          </section>
          <KindSummary diff={data} />
          <Entities diff={data} fromHash={from.rkp_hash} toHash={to.rkp_hash} onOpen={onOpenEntity} />
          <Relationships diff={data} fromHash={from.rkp_hash} toHash={to.rkp_hash} onOpen={onOpenEntity} />
          <Impact diff={data} toHash={to.rkp_hash} onOpen={onOpenEntity} />
        </>
      )}
    </div>
  );
}
