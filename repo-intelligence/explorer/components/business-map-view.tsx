"use client";

import { ArrowDown, ArrowUp, Plus, Sparkles, Trash2 } from "lucide-react";
import { useMemo, useState, type DragEvent } from "react";
import { api, useApi } from "@/lib/api";
import { starter, templates } from "@/lib/business-map-data";
import type { EvidenceExcerpt } from "@/lib/types";
import { isPlaced, placementLabel, type Capability, type MapDocument, type MapSubject, type Placement, type Stage, type WorkspaceMap } from "@/lib/workspace";
import { Badge, ErrorNote, Loading } from "./ui";
import "./workspace-tools.css";

type OpenEntity = (entityId: string, rkpHash?: string) => void;
type Review = (lineage: string, action: "confirm" | "move" | "remove" | "reject", capability?: string) => Promise<void>;

function stage(name = "New stage"): Stage {
  return { id: crypto.randomUUID(), name, capabilities: [] };
}

function revise(items: Stage[], index: number, direction: number): Stage[] {
  const next = [...items];
  const destination = index + direction;
  if (destination >= 0 && destination < next.length) {
    [next[index], next[destination]] = [next[destination], next[index]];
  }
  return next;
}

function MapEditor({ document, onSave, onCancel, busy }: {
  document: MapDocument; onSave: (title: string, stages: Stage[]) => Promise<void>; onCancel: () => void; busy: boolean;
}) {
  const [draft, setDraft] = useState({ title: document.title, stages: document.stages });
  const writeStages = (stages: Stage[]) => setDraft((old) => ({ ...old, stages }));
  const setStage = (id: string, change: (item: Stage) => Stage) => setDraft((old) => ({
    ...old, stages: old.stages.map((item) => item.id === id ? change(item) : item),
  }));
  function updateCapability(stageId: string, item: Capability) {
    setStage(stageId, (row) => ({ ...row, capabilities: row.capabilities.map((cap) => cap.id === item.id ? item : cap) }));
  }
  function moveCapability(cap: Capability, from: string, to: string) {
    writeStages(draft.stages.map((item) => item.id === from
      ? { ...item, capabilities: item.capabilities.filter((row) => row.id !== cap.id) }
      : item.id === to ? { ...item, capabilities: [...item.capabilities, cap] } : item));
  }
  function loadStarter() {
    const existing = new Set(draft.stages.flatMap((row) => row.capabilities.map((cap) => cap.id)));
    const additions = starter.filter((item) => !existing.has(item.id));
    if (additions.length) writeStages([...draft.stages, { ...stage("Starter capabilities"), capabilities: additions }]);
  }
  return (
    <form className="workspace-tool-form" onSubmit={(event) => { event.preventDefault(); void onSave(draft.title, draft.stages); }}>
      <div className="work-tool-panel work-tool-controls">
        <label className="field"><span>Map name</span><input value={draft.title} required maxLength={120} onChange={(event) => setDraft({ ...draft, title: event.target.value })} /></label>
        <label className="field"><span>Stage template</span>
          <select value="" onChange={(event) => { const names = templates[event.target.value]; if (names) writeStages([...draft.stages, ...names.map(stage)]); }}>
            <option value="">Add stages from a template</option>
            {Object.keys(templates).map((template) => <option key={template} value={template}>{template}</option>)}
          </select>
        </label>
        <button type="button" className="btn btn-quiet" onClick={loadStarter}>Load starter</button>
      </div>
      {draft.stages.map((item, index) => (
        <section className="work-tool-panel" key={item.id} aria-label={item.name || "Stage"}>
          <div className="work-tool-controls">
            <label className="field"><span>Stage name</span><input value={item.name} required maxLength={120} onChange={(e) => setStage(item.id, (row) => ({ ...row, name: e.target.value }))} /></label>
            <div className="work-tool-actions">
              <button type="button" className="icon-btn" aria-label={`Move ${item.name} earlier`} disabled={index === 0} onClick={() => writeStages(revise(draft.stages, index, -1))}><ArrowUp size={15} /></button>
              <button type="button" className="icon-btn" aria-label={`Move ${item.name} later`} disabled={index === draft.stages.length - 1} onClick={() => writeStages(revise(draft.stages, index, 1))}><ArrowDown size={15} /></button>
              <button type="button" className="icon-btn" aria-label={`Remove ${item.name} stage`} onClick={() => writeStages(draft.stages.filter((row) => row.id !== item.id))}><Trash2 size={15} /></button>
            </div>
          </div>
          <div className="work-tool-rows">
            {item.capabilities.map((cap) => (
              <div key={cap.id} className="work-tool-controls">
                <label className="field"><span>Capability name</span><input value={cap.name} required maxLength={120} onChange={(e) => updateCapability(item.id, { ...cap, name: e.target.value })} /></label>
                <label className="field"><span>Description</span><input value={cap.description} maxLength={500} onChange={(e) => updateCapability(item.id, { ...cap, description: e.target.value })} /></label>
                <label className="field"><span>Stage</span><select value={item.id} onChange={(e) => moveCapability(cap, item.id, e.target.value)}>{draft.stages.map((row) => <option key={row.id} value={row.id}>{row.name}</option>)}</select></label>
                <button type="button" className="icon-btn" aria-label={`Remove ${cap.name} capability`} onClick={() => setStage(item.id, (row) => ({ ...row, capabilities: row.capabilities.filter((c) => c.id !== cap.id) }))}><Trash2 size={15} /></button>
              </div>
            ))}
          </div>
          <button type="button" className="btn btn-quiet btn-small" onClick={() => setStage(item.id, (row) => ({ ...row, capabilities: [...row.capabilities, { id: crypto.randomUUID(), name: "New capability", description: "" }] }))}>
            <Plus size={14} /> Add capability
          </button>
        </section>
      ))}
      <div className="work-tool-actions">
        <button type="button" className="btn btn-quiet" onClick={() => writeStages([...draft.stages, stage()])}><Plus size={14} /> Add stage</button>
        <button type="button" className="btn btn-quiet" onClick={onCancel}>Cancel</button>
        <button type="submit" className="btn btn-primary" disabled={busy}>Save map</button>
      </div>
    </form>
  );
}

function Subject({ subject, placement, capabilities, busy, onReview, onOpen }: {
  subject: MapSubject; placement?: Placement; capabilities: Capability[]; busy: boolean; onReview: Review; onOpen: OpenEntity;
}) {
  const [target, setTarget] = useState("");
  const name = capabilities.find((cap) => cap.id === placement?.capability_id)?.name;
  const isAssigned = placement && isPlaced(placement);
  return (
    <li className="business-subject" draggable={!busy} onDragStart={(event) => {
      event.dataTransfer.setData("application/x-rkp-subject", subject.lineage_id);
      event.dataTransfer.effectAllowed = "move";
    }}>
      <div className="business-subject-meta">
        <button type="button" className="link-btn" onClick={() => onOpen(subject.entity_id, subject.rkp_hash)}>{subject.name}</button>
        <span>{subject.repository_name} · {subject.component_kind ?? subject.kind}</span>
      </div>
      {placement && <Badge tone={placement.status === "confirmed" ? "verified" : "neutral"}>{placementLabel(placement)}</Badge>}
      {!isAssigned && name && <p className="group-note">{name}</p>}
      {placement?.status === "declared" && placement.evidence_ids.slice(0, 2).map((id) => <SourceEvidence key={id} hash={subject.rkp_hash} id={id} />)}
      {!isAssigned && Boolean(placement?.candidates.length) && <p className="group-note">
        Candidates {placement?.candidates.map((id) => capabilities.find((cap) => cap.id === id)?.name).filter(Boolean).join(", ")}
      </p>}
      <div className="work-tool-actions">
        {placement?.capability_id && placement.status !== "confirmed" && <button type="button" className="btn btn-quiet btn-small" disabled={busy} onClick={() => void onReview(subject.lineage_id, "confirm")}>Confirm</button>}
        {placement?.capability_id && <button type="button" className="btn btn-quiet btn-small" disabled={busy} onClick={() => void onReview(subject.lineage_id, isAssigned ? "remove" : "reject")}>{isAssigned ? "Remove" : "Reject"}</button>}
        <select aria-label={`Capability for ${subject.name}`} value={target} onChange={(event) => setTarget(event.target.value)}>
          <option value="">Choose capability</option>
          {capabilities.map((cap) => <option key={cap.id} value={cap.id}>{cap.name}</option>)}
        </select>
        <button type="button" className="btn btn-quiet btn-small" disabled={busy || !target} onClick={() => void onReview(subject.lineage_id, "move", target)}>{isAssigned ? "Move" : "Place"}</button>
      </div>
    </li>
  );
}

function SourceEvidence({ hash, id }: { hash: string; id: string }) {
  const [open, setOpen] = useState(false);
  const data = useApi<EvidenceExcerpt>(open ? `evidence/${id}?rkp_hash=${encodeURIComponent(hash)}` : null);
  return <details onToggle={(event) => setOpen(event.currentTarget.open)}><summary>Source evidence</summary>
    {data.loading && <Loading label="Loading source evidence" />}
    {data.error && <ErrorNote error={data.error} onRetry={data.reload} what="source evidence" />}
    {data.data && <>
      <p className="group-note">{data.data.paths.join(", ")}</p>
      {data.data.text === null ? <p className="group-note">Source unavailable.</p> : <pre className="business-evidence">{data.data.text}</pre>}
    </>}
  </details>;
}

export function BusinessMapView({ onOpen }: { onOpen: OpenEntity }) {
  const source = useApi<WorkspaceMap>("business-maps");
  const revisions = useApi<{ revisions: { version: number; author: string; updated_at: string }[] }>("business-maps/revisions");
  const [response, setResponse] = useState<WorkspaceMap>();
  const [editing, setEditing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  const [showPins, setShowPins] = useState(false);
  const [showExcluded, setShowExcluded] = useState(false);
  const data = response ?? source.data;
  const byPlacement = useMemo(() => new Map(data?.map.placements.map((row) => [row.lineage_id, row])), [data]);
  const names = useMemo(() => new Map(data?.map.stages.flatMap((row) => row.capabilities.map((cap) => [cap.id, cap.name] as const))), [data]);
  if (source.loading && !data) return <Loading label="Loading business map" />;
  if (source.error && !data) return <ErrorNote error={source.error} onRetry={source.reload} what="the business map" />;
  if (!data) return null;

  const capabilities = data.map.stages.flatMap((row) => row.capabilities);
  const active = new Set(data.subjects.map((subject) => subject.lineage_id));
  const orphaned = data.map.placements.filter((row) => row.capability_id && !active.has(row.lineage_id));
  const tray = data.subjects.filter((subject) => {
    const row = byPlacement.get(subject.lineage_id);
    return (!row || !isPlaced(row)) && (data.eligible_kinds.includes(subject.component_kind ?? "") || row?.capability_id || (showPins && subject.kind !== "Component") || (showExcluded && subject.kind === "Component"));
  });

  async function request(path: string, body: unknown): Promise<boolean> {
    setBusy(true);
    setError(undefined);
    try {
      setResponse(await api<WorkspaceMap>(path, { body }));
      revisions.reload();
      return true;
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Business map could not be saved.");
      return false;
    } finally {
      setBusy(false);
    }
  }
  const review: Review = async (lineage, action, capability) => {
    await request("business-maps/placements", { expected_version: data.map.version, lineage_id: lineage, action, capability_id: capability });
  };
  function drop(event: DragEvent, capability: string) {
    event.preventDefault();
    const id = event.dataTransfer.getData("application/x-rkp-subject");
    if (!busy && active.has(id)) void review(id, "move", capability);
  }
  return (
    <section className="work-tool">
      <header className="work-tool-header">
        <div><h2>{data.map.title}</h2><p>Group repository components by the capabilities they support.</p></div>
        <div className="work-tool-actions">
          <button type="button" className="btn btn-quiet" onClick={() => { setResponse(undefined); source.reload(); revisions.reload(); }} disabled={busy || editing}>Reload</button>
          {!editing && <button type="button" className="btn btn-quiet" disabled={busy || !capabilities.length} onClick={() => void request("business-maps/run-placement", {})}><Sparkles size={14} /> Run placement</button>}
          {!editing && <button type="button" className="btn btn-primary" disabled={busy} onClick={() => setEditing(true)}>{data.map.version ? "Edit map" : "Create map"}</button>}
        </div>
      </header>
      {busy && <p role="status" className="note">Updating business map…</p>}
      {error && <p role="alert" className="form-error">{error}</p>}
      {editing ? <MapEditor document={data.map} busy={busy} onCancel={() => setEditing(false)} onSave={async (title, stages) => {
        if (await request("business-maps", { expected_version: data.map.version, title, stages })) setEditing(false);
      }} /> : <>
        {data.map.stages.length === 0 && <p className="group-note">Create stages and capabilities to start mapping components.</p>}
        <div className="business-lanes" aria-label="Value chain">
          {data.map.stages.map((item) => (
            <section className="business-lane" key={item.id}>
              <h3>{item.name}</h3>
              {item.capabilities.length === 0 && <p className="group-note">No capabilities yet.</p>}
              {item.capabilities.map((cap) => {
                const subjects = data.subjects.filter((subject) => {
                  const row = byPlacement.get(subject.lineage_id);
                  return row?.capability_id === cap.id && isPlaced(row);
                });
                const technical = [...new Set(subjects.flatMap((subject) => subject.technical_capabilities))].sort();
                return <section key={cap.id} id={`capability-${cap.id}`} className="business-capability" aria-label={cap.name}
                  onDragOver={(event) => { if (event.dataTransfer.types.includes("application/x-rkp-subject")) event.preventDefault(); }}
                  onDrop={(event) => drop(event, cap.id)}>
                  <h4>{cap.name}</h4><p>{cap.description || "Placement uses the capability name."}</p>
                  {technical.length > 0 && <p className="group-note">{technical.join(" · ")}</p>}
                  <details className="business-cap-id"><summary>Capability ID</summary><code>{cap.id}</code></details>
                  {data.dependencies.filter((row) => row.from === cap.id).map((edge) => <p key={edge.to} className="group-note">
                    <a href={`#capability-${edge.to}`}>→ {names.get(edge.to)}</a> <Badge tone={edge.resolution === "exact" ? "verified" : "candidate"}>{edge.resolution}</Badge>
                  </p>)}
                  {subjects.length > 0 && <ul className="business-subjects">{subjects.map((subject) => <Subject key={subject.lineage_id} subject={subject} placement={byPlacement.get(subject.lineage_id)} capabilities={capabilities} busy={busy} onReview={review} onOpen={onOpen} />)}</ul>}
                </section>;
              })}
            </section>
          ))}
        </div>
        {capabilities.length > 0 && <section className="work-tool-panel">
          <div className="work-tool-controls"><h3>Needs placing</h3>
            <label className="work-check"><input type="checkbox" checked={showPins} onChange={(event) => setShowPins(event.target.checked)} /> Show endpoints and tables to pin</label>
            <label className="work-check"><input type="checkbox" checked={showExcluded} onChange={(event) => setShowExcluded(event.target.checked)} /> Show other component kinds</label>
          </div>
          {tray.length ? <ul className="business-subjects">{tray.map((subject) => <Subject key={subject.lineage_id} subject={subject} placement={byPlacement.get(subject.lineage_id)} capabilities={capabilities} busy={busy} onReview={review} onOpen={onOpen} />)}</ul> : <p className="group-note">No subjects need review.</p>}
        </section>}
        {orphaned.length > 0 && <section className="work-tool-panel">
          <h3>Orphaned placements</h3>
          {orphaned.map((row) => <p key={row.lineage_id}><code>{row.lineage_id}</code> · {names.get(row.capability_id ?? "")} <button type="button" className="btn btn-quiet btn-small" disabled={busy} onClick={() => void review(row.lineage_id, "remove")}>Remove</button></p>)}
        </section>}
        {data.dependencies.length > 0 && <section className="work-tool-panel">
          <h3>Capability dependencies</h3>
          {data.dependencies.map((row) => <details key={`${row.from}:${row.to}`}><summary>{names.get(row.from)} → {names.get(row.to)} <Badge tone={row.resolution === "exact" ? "verified" : "candidate"}>{row.resolution}</Badge></summary>
            <ul>{row.evidence.map((item) => <li key={`${item.rkp_hash}:${item.assertion_id}`}>{item.predicate} <code>{item.assertion_id}</code>
              {item.evidence_ids.slice(0, 3).map((id) => <SourceEvidence key={id} hash={item.rkp_hash} id={id} />)}
            </li>)}</ul>
          </details>)}
        </section>}
        {data.cross_repository_unknowns.length > 0 && <details className="work-tool-panel"><summary>Unresolved cross-repository calls ({data.cross_repository_unknowns.length})</summary><ul>
          {data.cross_repository_unknowns.map((call) => <li key={`${call.rkp_hash}:${call.subject}:${call.evidence_ids.join(",")}`}><code>{call.path}</code> · {call.reason.replaceAll("_", " ")}</li>)}
        </ul></details>}
      </>}
      {(revisions.data?.revisions.length ?? 0) > 0 && <details className="work-tool-panel"><summary>Revision history</summary><ol>
        {revisions.data?.revisions.map((row) => <li key={row.version}>Version {row.version} · {row.author} · <time dateTime={row.updated_at}>{new Date(row.updated_at).toLocaleString()}</time></li>)}
      </ol></details>}
    </section>
  );
}
