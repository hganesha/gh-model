"use client";

import "./workspace-tools.css";

import { ArrowLeft, Search } from "lucide-react";
import { useEffect, useState } from "react";
import { snapshotPath, useApi } from "@/lib/api";
import { entityContext, kindLabel, plural } from "@/lib/format";
import { domainLabel, kindsForPlane, PLANE_BY_ID } from "@/lib/planes";
import type { EntitiesResponse, Entity, Summary } from "@/lib/types";
import { Coverage, ErrorNote, Loading, PlaneIcon } from "./ui";

const GROUP_LIMIT = 60;
const KIND_LIMIT = 500;

function useDebounced<T>(value: T, ms: number): T {
  const [v, setV] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return v;
}

export function EntityRow({ entity, onOpen, active }: { entity: Entity; onOpen: (id: string) => void; active?: boolean }) {
  const context = entityContext(entity);
  return (
    <button type="button" className={`entity-row ${active ? "is-active" : ""}`} onClick={() => onOpen(entity.id)}>
      <span className="entity-name">{entity.name}</span>
      {context && <span className="entity-context">{context}</span>}
    </button>
  );
}

function KindGroup({
  hash,
  plane,
  kind,
  count,
  q,
  limit,
  onOpenEntity,
  onShowAll,
  activeId,
}: {
  hash: string;
  plane: string;
  kind: string;
  count: number;
  q: string;
  limit: number;
  onOpenEntity: (id: string) => void;
  onShowAll?: () => void;
  activeId: string | null;
}) {
  const params = new URLSearchParams({ plane, kind, limit: String(limit) });
  if (q) params.set("q", q);
  // A kind the summary counted as zero is not fetched: the summary already says none were found.
  const skip = count === 0;
  const { data, error, loading, reload } = useApi<EntitiesResponse>(skip ? null : snapshotPath(hash, `entities?${params}`));

  return (
    <section className="kind-group" aria-label={kindLabel(kind)}>
      <header>
        <h3>{kindLabel(kind)}</h3>
        <span className="kind-group-count">{count.toLocaleString("en-US")}</span>
      </header>
      {skip && <p className="group-note">None found in this snapshot.</p>}
      {loading && <Loading label={`Loading ${kindLabel(kind).toLowerCase()} entities`} />}
      {error && <ErrorNote error={error} onRetry={reload} what={`${kindLabel(kind)} entities`} />}
      {data && data.entities.length === 0 && <p className="group-note">{q ? "No entities of this kind match the filter." : "None found in this snapshot."}</p>}
      {data && data.entities.length > 0 && (
        <>
          <div className="entity-list">
            {data.entities.map((e) => (
              <EntityRow key={e.id} entity={e} onOpen={onOpenEntity} active={e.id === activeId} />
            ))}
          </div>
          {data.truncated && (
            <p className="group-note group-truncated">
              Showing first {data.entities.length.toLocaleString("en-US")}
              {!q && count > data.entities.length ? ` of ${count.toLocaleString("en-US")}` : ""}.{" "}
              {onShowAll ? (
                <button type="button" className="link-btn" onClick={onShowAll}>
                  Show more of this kind
                </button>
              ) : (
                "Narrow the filter to see the rest."
              )}
            </p>
          )}
        </>
      )}
    </section>
  );
}

function DependencyGroups({ hash, q, onOpen }: { hash: string; q: string; onOpen: (id: string) => void }) {
  const { data, error, loading, reload } = useApi<{ groups: { capability: Entity; dependencies: Entity[] }[]; unclassified: Entity[]; overlaps?: { component: Entity; capability: Entity; dependencies: Entity[] }[] }>(snapshotPath(hash, "dependencies"));
  if (loading) return <Loading label="Loading dependency capabilities" />;
  if (error) return <ErrorNote error={error} onRetry={reload} what="dependency capabilities" />;
  if (!data) return null;
  const query = q.toLowerCase();
  const groups = data.groups.map((row) => ({ ...row, dependencies: row.dependencies.filter((dep) => (dep.name + row.capability.name).toLowerCase().includes(query)) })).filter((row) => row.dependencies.length);
  const other = data.unclassified.filter((row) => row.name.toLowerCase().includes(query));
  return <section className="kind-group" aria-label="Dependencies by capability">
    <header><h3>Dependencies by capability</h3></header>
    {(data.overlaps?.length ?? 0) > 0 && <>
      <p className="group-note">More than one package for the same capability inside one component. They may be complementary; check before treating them as duplicates.</p>
      <ul className="overlap-list">{data.overlaps?.map((row) => <li key={`${row.component.id}:${row.capability.id}`}>
        <button type="button" className="link-btn" onClick={() => onOpen(row.component.id)}>{row.component.name}</button> uses {row.dependencies.map((dep) => dep.name).join(", ")} for {row.capability.name}
      </li>)}</ul>
    </>}
    {groups.map((row) => <section key={row.capability.id} className="work-tool-row">
      <button type="button" className="link-btn" onClick={() => onOpen(row.capability.id)}>{row.capability.name}</button>
      <div className="entity-list">{row.dependencies.map((dep) => <EntityRow key={dep.id} entity={dep} onOpen={onOpen} />)}</div>
    </section>)}
    {other.length > 0 && <section className="work-tool-row"><strong>Unclassified</strong><div className="entity-list">{other.map((dep) => <EntityRow key={dep.id} entity={dep} onOpen={onOpen} />)}</div></section>}
    {groups.length === 0 && other.length === 0 && <p className="group-note">No matching dependencies.</p>}
  </section>;
}

export function PlaneView({
  hash,
  planeId,
  summary,
  onBack,
  onOpenEntity,
  activeEntityId,
}: {
  hash: string;
  planeId: string;
  summary: Summary;
  onBack: () => void;
  onOpenEntity: (id: string) => void;
  activeEntityId: string | null;
}) {
  const meta = PLANE_BY_ID[planeId];
  const isFiles = planeId === "files";
  const kinds = meta ? kindsForPlane(summary, meta) : [{ kind: "Artifact", count: summary.files ?? 0 }];
  const total = kinds.reduce((n, k) => n + k.count, 0);
  const [kind, setKind] = useState<string | null>(null);
  const [text, setText] = useState("");
  const q = useDebounced(text.trim(), 250);

  const shown = kind ? kinds.filter((k) => k.kind === kind) : kinds;

  return (
    <section className={`plane-view plane-${planeId}`} aria-label={meta ? meta.title : "Files"}>
      <button type="button" className="back-link" onClick={onBack}>
        <ArrowLeft size={14} aria-hidden="true" /> Map
      </button>
      <header className="plane-view-head">
        <span className="plane-icon plane-icon-large">
          <PlaneIcon plane={planeId} size={20} />
        </span>
        <div>
          <span className="eyebrow">{meta ? meta.eyebrow : "FILES"}</span>
          <h2>{meta ? meta.title : "Files"}</h2>
          <p>{meta ? meta.blurb : "Every file the compiler inventoried for this commit."}</p>
        </div>
        <div className="plane-view-coverage">
          {meta?.coverage.map((d) => <Coverage key={d} label={domainLabel(d)} ratio={summary.coverage_totals?.[d]} />)}
          {isFiles && <Coverage label={domainLabel("parse")} ratio={summary.coverage_totals?.parse} />}
        </div>
      </header>

      {total === 0 ? (
        <div className="empty-plane">
          <h3>Nothing was found for this plane in this snapshot.</h3>
          <p>
            The compiler looked for {kinds.map((k) => kindLabel(k.kind).toLowerCase()).join(", ")} entities and recorded none. This is not proof of absence: the repository may express them in a
            way the compiler does not recognise yet.
          </p>
        </div>
      ) : (
        <>
          <div className="filters">
            <label className="text-filter">
              <Search size={14} aria-hidden="true" />
              <span className="sr-only">Filter entities by text</span>
              <input type="search" value={text} onChange={(e) => setText(e.target.value)} placeholder="Filter by name" autoComplete="off" spellCheck={false} />
            </label>
            {kinds.length > 1 && (
              <div className="chips" role="group" aria-label="Filter by kind">
                <button type="button" className={`chip ${kind === null ? "is-on" : ""}`} aria-pressed={kind === null} onClick={() => setKind(null)}>
                  All <i>{total.toLocaleString("en-US")}</i>
                </button>
                {kinds.map((k) => (
                  <button type="button" key={k.kind} className={`chip ${kind === k.kind ? "is-on" : ""} ${k.count === 0 ? "chip-zero" : ""}`} aria-pressed={kind === k.kind} onClick={() => setKind(k.kind)}>
                    {kindLabel(k.kind)} <i>{k.count.toLocaleString("en-US")}</i>
                  </button>
                ))}
              </div>
            )}
          </div>
          <p className="sr-only" aria-live="polite">
            {q ? `Filtering ${plural(total, "entity", "entities")} by ${q}` : ""}
          </p>
          <div className="kind-groups">
            {shown.map((k) => k.kind === "ExternalDependency" ? <DependencyGroups key={k.kind} hash={hash} q={q} onOpen={onOpenEntity} /> : (
              <KindGroup
                key={k.kind}
                hash={hash}
                plane={planeId}
                kind={k.kind}
                count={k.count}
                q={q}
                limit={kind || kinds.length === 1 ? KIND_LIMIT : GROUP_LIMIT}
                onOpenEntity={onOpenEntity}
                onShowAll={kind || kinds.length === 1 ? undefined : () => setKind(k.kind)}
                activeId={activeEntityId}
              />
            ))}
          </div>
        </>
      )}
    </section>
  );
}
