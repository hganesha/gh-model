"use client";

import { ArrowLeft, ArrowRight, CircleHelp, EyeOff, FileCode2, Share2, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { api, ApiError, snapshotPath, useApi } from "@/lib/api";
import { placementLabel, type SnapshotPlacements } from "@/lib/workspace";
import { displayValue, kindLabel, plural, shortId } from "@/lib/format";
import { planeOfKind } from "@/lib/planes";
import type { Assertion, EntityDetail, EntityRef, EvidenceExcerpt, Unknown } from "@/lib/types";
import { AssertionBadges, Badge, ErrorNote, isTentative, Loading } from "./ui";

function Definitions({ data, empty }: { data: Record<string, unknown>; empty: string }) {
  const entries = Object.entries(data ?? {});
  if (!entries.length) return <p className="group-note">{empty}</p>;
  return (
    <dl className="defs">
      {entries.map(([key, value]) => (
        <div key={key}>
          <dt>{key}</dt>
          <dd className={value === null || value === undefined ? "is-null" : ""}>{displayValue(value)}</dd>
        </div>
      ))}
    </dl>
  );
}

type SourceState = { loading: true } | { loading: false; error: string } | { loading: false; excerpt: EvidenceExcerpt };

/**
 * One evidence item. The excerpt is requested only when the user presses
 * "Show source", lives only in this component's state, and is dropped when the
 * item is hidden or the drawer moves on.
 */
function EvidenceItem({ hash, evidenceId, usedBy }: { hash: string; evidenceId: string; usedBy: string }) {
  const [source, setSource] = useState<SourceState | null>(null);
  const request = useRef<AbortController | null>(null);

  useEffect(() => () => request.current?.abort(), []);

  async function show() {
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setSource({ loading: true });
    try {
      const excerpt = await api<EvidenceExcerpt>(`evidence/${encodeURIComponent(evidenceId)}?rkp_hash=${encodeURIComponent(hash)}`, { signal: controller.signal });
      setSource({ loading: false, excerpt });
    } catch (err) {
      if (controller.signal.aborted) return;
      setSource({ loading: false, error: err instanceof ApiError ? err.message : "The excerpt could not be loaded." });
    }
  }

  function hide() {
    request.current?.abort();
    setSource(null);
  }

  const excerpt = source && !source.loading && "excerpt" in source ? source.excerpt : null;
  const span = excerpt?.span;
  const lines = span?.start_line ? (span.end_line && span.end_line !== span.start_line ? `${span.start_line}–${span.end_line}` : `${span.start_line}`) : null;

  return (
    <li className="evidence-item">
      <div className="evidence-head">
        <FileCode2 size={14} aria-hidden="true" />
        <div>
          <code>{shortId(evidenceId)}</code>
          <span>{usedBy}</span>
        </div>
        {source === null ? (
          <button type="button" className="btn btn-quiet btn-small" onClick={show}>
            Show source
          </button>
        ) : (
          <button type="button" className="btn btn-quiet btn-small" onClick={hide}>
            Hide source
          </button>
        )}
      </div>
      {source?.loading && <Loading label="Fetching excerpt" />}
      {source && !source.loading && "error" in source && <ErrorNote error={{ message: source.error }} onRetry={show} what="the excerpt" />}
      {excerpt && (
        <figure className="excerpt">
          <figcaption>
            <span className="excerpt-path">
              {excerpt.paths?.length ? excerpt.paths.join(", ") : "path unknown"}
              {lines ? `:${lines}` : ""}
            </span>
            <Badge tone="trust" title="How far this text should be trusted">
              {excerpt.trust_label ?? "trust unknown"}
            </Badge>
          </figcaption>
          {excerpt.status !== "ok" || excerpt.text === null || excerpt.text === undefined ? (
            <p className="group-note">{excerpt.status === "source_unavailable" ? "The source for this evidence is not available." : `Source status: ${excerpt.status}`}</p>
          ) : (
            <pre tabIndex={0}>{excerpt.text}</pre>
          )}
          {excerpt.masked && (
            <p className="excerpt-flag">
              <EyeOff size={13} aria-hidden="true" /> Secret redacted — part of this excerpt was masked before it left the server.
            </p>
          )}
          {excerpt.truncated && <p className="excerpt-flag">Excerpt truncated.</p>}
        </figure>
      )}
    </li>
  );
}

interface RelationGroup {
  key: string;
  predicate: string;
  outgoing: boolean;
  items: { assertion: Assertion; otherId: string }[];
}

function groupRelations(id: string, assertions: Assertion[]): RelationGroup[] {
  const groups = new Map<string, RelationGroup>();
  for (const a of assertions) {
    const outgoing = a.subject === id;
    const key = `${a.predicate}:${outgoing ? "out" : "in"}`;
    let g = groups.get(key);
    if (!g) {
      g = { key, predicate: a.predicate, outgoing, items: [] };
      groups.set(key, g);
    }
    g.items.push({ assertion: a, otherId: outgoing ? a.object : a.subject });
  }
  return [...groups.values()].sort((a, b) => Number(b.outgoing) - Number(a.outgoing) || a.predicate.localeCompare(b.predicate));
}

function Relationships({ id, detail, onNavigate }: { id: string; detail: EntityDetail; onNavigate: (id: string) => void }) {
  const groups = groupRelations(id, detail.assertions);
  if (!groups.length) {
    return <p className="group-note">No relationships were recorded for this entity in this snapshot.</p>;
  }
  return (
    <div className="relations">
      {groups.map((g) => (
        <div className="relation-group" key={g.key}>
          <h4>
            {g.outgoing ? <ArrowRight size={12} aria-hidden="true" /> : <ArrowLeft size={12} aria-hidden="true" />}
            <span className="predicate">{g.predicate}</span>
            <span className="direction">{g.outgoing ? "outgoing" : "incoming"}</span>
            <span className="relation-count">{g.items.length}</span>
          </h4>
          <ul>
            {g.items.map(({ assertion, otherId }) => {
              const other: EntityRef | undefined = detail.neighbours?.[otherId];
              return (
                <li key={assertion.id} className={isTentative(assertion) ? "is-tentative" : ""}>
                  {other ? (
                    <button type="button" className="neighbour" onClick={() => onNavigate(other.id)}>
                      <i className={`dot plane-${planeOfKind(other.kind)}`} aria-hidden="true" />
                      <span className="neighbour-name">{other.name}</span>
                      <span className="neighbour-kind">{kindLabel(other.kind)}</span>
                    </button>
                  ) : (
                    <span className="neighbour neighbour-literal">
                      <span className="neighbour-name">{otherId}</span>
                    </span>
                  )}
                  <AssertionBadges assertion={assertion} />
                </li>
              );
            })}
          </ul>
        </div>
      ))}
    </div>
  );
}

function evidenceIndex(detail: EntityDetail): { id: string; usedBy: string }[] {
  const uses = new Map<string, Set<string>>();
  const add = (id: string, label: string) => {
    if (!uses.has(id)) uses.set(id, new Set());
    uses.get(id)!.add(label);
  };
  for (const id of detail.entity.evidence_ids ?? []) add(id, "entity");
  for (const a of detail.assertions) for (const id of a.evidence_ids ?? []) add(id, a.predicate);
  return [...uses.entries()].map(([id, labels]) => ({ id, usedBy: `supports ${[...labels].join(", ")}` }));
}

const EVIDENCE_PAGE = 12;

function BusinessPlacement({ hash, id, onChanged }: { hash: string; id: string; onChanged: () => void }) {
  const source = useApi<SnapshotPlacements>(snapshotPath(hash, "placements"));
  const row = source.data?.components[id];
  const lineageId = row?.lineage_id;
  const [target, setTarget] = useState("");
  const [problem, setProblem] = useState("");
  const [busy, setBusy] = useState(false);
  if (!row || !source.data) return null;
  async function change(action: "move" | "confirm") {
    setBusy(true);
    setProblem("");
    try {
      await api("business-maps/placements", { body: {
        expected_version: source.data?.version, lineage_id: lineageId, action,
        capability_id: action === "move" ? target : undefined,
      } });
      source.reload();
      onChanged();
    } catch (failure) {
      setProblem(failure instanceof Error ? failure.message : "Placement could not be saved.");
    } finally {
      setBusy(false);
    }
  }
  return <section>
    <h3>Business capability</h3>
    {row.capability && <p>Supports {row.capability}</p>}
    <Badge tone={row.status === "confirmed" ? "verified" : "neutral"}>{placementLabel(row)}</Badge>
    <div className="work-tool-actions">
      {row.capability_id && row.status !== "confirmed" && <button type="button" className="btn btn-quiet btn-small" disabled={busy} onClick={() => void change("confirm")}>Confirm placement</button>}
      <label><span className="sr-only">Move to capability</span><select value={target} onChange={(event) => setTarget(event.target.value)}>
        <option value="">Choose a capability</option>
        {source.data.capabilities?.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
      </select></label>
      <button type="button" className="btn btn-quiet btn-small" disabled={busy || !target} onClick={() => void change("move")}>Move</button>
    </div>
    {problem && <p className="form-error" role="alert">{problem}</p>}
  </section>;
}

function DrawerBody({ hash, id, onNavigate, onViewInGraph, onPlacementChanged }: { hash: string; id: string; onNavigate: (id: string) => void; onViewInGraph: (ref: EntityRef) => void; onPlacementChanged: () => void }) {
  const { data, error, loading, reload } = useApi<EntityDetail>(snapshotPath(hash, `entities/${encodeURIComponent(id)}`));
  const [evidenceShown, setEvidenceShown] = useState(EVIDENCE_PAGE);

  if (loading) return <Loading label="Loading entity" />;
  if (error) return <ErrorNote error={error} onRetry={reload} what="this entity" />;
  if (!data) return null;

  const { entity } = data;
  const evidence = evidenceIndex(data);
  const plane = planeOfKind(entity.kind);
  const provided = data.assertions.filter((row) => row.predicate === "PROVIDES" && row.subject === id);

  return (
    <>
      <div className={`drawer-title plane-${plane}`}>
        <span className="kind-tag">
          <i className={`dot plane-${plane}`} aria-hidden="true" />
          {kindLabel(entity.kind)}
        </span>
        <h2 id="drawer-heading">{entity.name}</h2>
        <button type="button" className="btn btn-quiet btn-small" onClick={() => onViewInGraph({ id: entity.id, kind: entity.kind, name: entity.name })}>
          <Share2 size={13} aria-hidden="true" /> View in graph
        </button>
      </div>

      {entity.kind === "Component" && <BusinessPlacement hash={hash} id={id} onChanged={onPlacementChanged} />}
      {provided.length > 0 && <section><h3>Technical capabilities</h3>
        <ul>{provided.map((row) => <li key={row.id}><button type="button" className="link-btn" onClick={() => onNavigate(row.object)}>{data.neighbours[row.object]?.name ?? row.object}</button></li>)}</ul>
        <p className="group-note">Curated from the declared package.</p>
      </section>}
      <section>
        <h3>Natural key</h3>
        <Definitions data={entity.natural_key} empty="This entity has no natural key fields." />
      </section>

      <section>
        <h3>Attributes</h3>
        <Definitions data={entity.attributes} empty="No attributes recorded." />
      </section>

      <section>
        <h3>Relationships</h3>
        {data.truncated && <p className="group-note group-truncated">Truncated by the server: only part of this entity&apos;s relationships is shown.</p>}
        <Relationships id={id} detail={data} onNavigate={onNavigate} />
      </section>

      <section>
        <h3>
          Unknowns <span className="section-count">{(data.unknowns ?? []).length}</span>
        </h3>
        {(data.unknowns ?? []).length === 0 ? (
          <p className="group-note">No unknowns recorded for this entity.</p>
        ) : (
          <div className="relations">
            <p className="group-note">Relationships the compiler expected here but could not resolve. Unknown is not the same as absent.</p>
            {data.unknowns.map((u: Unknown) => (
              <div className="relation-group relation-unknown" key={u.id}>
                <h4>
                  <CircleHelp size={12} aria-hidden="true" />
                  <span className="predicate">{u.predicate}</span>
                  <span className="direction">target {u.status || "unknown"}</span>
                </h4>
                <p>{u.reason}</p>
              </div>
            ))}
          </div>
        )}
      </section>

      <section>
        <h3>Conflicts</h3>
        {(data.conflicts ?? []).length === 0 ? (
          <p className="group-note">No conflicts recorded for this entity.</p>
        ) : (
          <ul className="conflict-list">
            {data.conflicts.map((c) => (
              <li key={c.id}>
                <Badge tone="candidate">{c.conflict_type}</Badge>
                <span>{displayValue(c.basis)}</span>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section>
        <h3>
          Evidence <span className="section-count">{evidence.length}</span>
        </h3>
        {evidence.length === 0 ? (
          <p className="group-note">No evidence is attached to this entity or its relationships.</p>
        ) : (
          <>
            <p className="group-note">Source text is repository content. It is fetched only when you ask for it and shown as plain text.</p>
            <ul className="evidence-list">
              {evidence.slice(0, evidenceShown).map((e) => (
                <EvidenceItem key={e.id} hash={hash} evidenceId={e.id} usedBy={e.usedBy} />
              ))}
            </ul>
            {evidence.length > evidenceShown && (
              <button type="button" className="link-btn" onClick={() => setEvidenceShown((n) => n + EVIDENCE_PAGE)}>
                Show more evidence ({plural(evidence.length - evidenceShown, "item")} hidden)
              </button>
            )}
          </>
        )}
      </section>

      <p className="drawer-id">
        <code>{entity.id}</code>
      </p>
    </>
  );
}

export function EntityDrawer({
  hash,
  snapshotLabel,
  otherSnapshot,
  trail,
  onNavigate,
  onBack,
  onClose,
  onViewInGraph,
  onPlacementChanged,
}: {
  hash: string;
  /** Short commit and ref of the snapshot this entity is read from. */
  snapshotLabel: string;
  /** True when that snapshot is not the one selected in the sidebar. */
  otherSnapshot: boolean;
  trail: string[];
  onNavigate: (id: string) => void;
  onBack: () => void;
  onClose: () => void;
  onViewInGraph: (ref: EntityRef) => void;
  onPlacementChanged: () => void;
}) {
  const id = trail[trail.length - 1];
  const closeRef = useRef<HTMLButtonElement>(null);
  const opener = useRef<HTMLElement | null>(null);

  // Move focus into the drawer when it opens and give it back on close.
  useEffect(() => {
    opener.current = document.activeElement as HTMLElement | null;
    closeRef.current?.focus();
    return () => {
      if (opener.current && document.contains(opener.current)) opener.current.focus();
    };
  }, []);

  return (
    <aside className="drawer" role="dialog" aria-labelledby="drawer-heading" aria-label="Entity details">
      <header className="drawer-bar">
        {trail.length > 1 ? (
          <button type="button" className="btn btn-quiet btn-small" onClick={onBack}>
            <ArrowLeft size={13} aria-hidden="true" /> Back
          </button>
        ) : (
          <span className="eyebrow">ENTITY</span>
        )}
        <span className={`drawer-snapshot ${otherSnapshot ? "is-other" : ""}`}>
          <span className="sr-only">Shown as recorded in snapshot </span>
          <code>{snapshotLabel}</code>
        </span>
        <button ref={closeRef} type="button" className="icon-btn" onClick={onClose} aria-label="Close entity details">
          <X size={16} aria-hidden="true" />
        </button>
      </header>
      {/* Keyed by entity: moving to another entity discards any excerpts that were open. */}
      {otherSnapshot && (
        <p className="drawer-other" role="note">
          Showing this entity as recorded in snapshot <code>{snapshotLabel}</code>, which is not the snapshot selected in the sidebar.
        </p>
      )}
      <div className="drawer-scroll" key={`${hash}:${id}`}>
        <DrawerBody hash={hash} id={id} onNavigate={onNavigate} onViewInGraph={onViewInGraph} onPlacementChanged={onPlacementChanged} />
      </div>
    </aside>
  );
}
