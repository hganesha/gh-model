"use client";

import { ArrowLeft, ArrowRight, Check, Columns3, GripVertical, LayoutDashboard, Library, Move, Plus, Sparkles, Trash2, X } from "lucide-react";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type CSSProperties, type DragEvent, type ReactNode, type RefObject } from "react";
import { api, useApi } from "@/lib/api";
import { starterGroups, templates } from "@/lib/business-map-data";
import type { EvidenceExcerpt } from "@/lib/types";
import {
  isPlaced, placementBlocked, placementLabel, placementReason,
  type Capability, type MapDocument, type MapSubject, type Placement, type Stage, type WorkspaceMap,
} from "@/lib/workspace";
import { Badge, ErrorNote, Loading } from "./ui";
import "./workspace-tools.css";

type OpenEntity = (entityId: string, rkpHash?: string) => void;
type Review = (lineage: string, action: "confirm" | "move" | "remove" | "reject", capability?: string) => Promise<void>;

const SUBJECT_DRAG = "application/x-rkp-subject";
const CAPABILITY_DRAG = "application/x-rkp-capability";
const NO_EDGES: WorkspaceMap["dependencies"] = [];

function newStage(name = "New stage"): Stage {
  return { id: crypto.randomUUID(), name, capabilities: [] };
}

function swap(items: Stage[], index: number, direction: number): Stage[] {
  const next = [...items];
  const destination = index + direction;
  if (destination >= 0 && destination < next.length) {
    [next[index], next[destination]] = [next[destination], next[index]];
  }
  return next;
}

/* ---------- starter picker ---------- */

function StarterPicker({ stages, taken, onAdd, onClose }: {
  stages: Stage[]; taken: Set<string>; onAdd: (stageId: string, items: Capability[]) => void; onClose: () => void;
}) {
  const [query, setQuery] = useState("");
  const [target, setTarget] = useState(stages[0]?.id ?? "");
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const needle = query.trim().toLowerCase();
  const groups = starterGroups
    .map((group) => ({
      ...group,
      capabilities: group.capabilities.filter((item) => !taken.has(item.id) && (!needle || `${item.name} ${item.description}`.toLowerCase().includes(needle))),
    }))
    .filter((group) => group.capabilities.length);
  const chosen = starterGroups.flatMap((group) => group.capabilities).filter((item) => picked.has(item.id));
  function toggle(id: string, on: boolean) {
    setPicked((old) => {
      const next = new Set(old);
      if (on) next.add(id); else next.delete(id);
      return next;
    });
  }
  return (
    <section className="work-tool-panel starter-picker" aria-label="Starter capabilities">
      <div className="work-tool-controls">
        <label className="field"><span>Search starter capabilities</span><input type="search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="billing, logistics, analytics…" /></label>
        <label className="field"><span>Add to stage</span>
          <select value={target} onChange={(event) => setTarget(event.target.value)}>
            {stages.map((row) => <option key={row.id} value={row.id}>{row.name}</option>)}
          </select>
        </label>
        <div className="work-tool-actions">
          <button type="button" className="btn btn-primary" disabled={!chosen.length || !target} onClick={() => { onAdd(target, chosen); setPicked(new Set()); }}>
            <Plus size={14} aria-hidden="true" /> Add {chosen.length || ""} selected
          </button>
          <button type="button" className="btn btn-quiet" onClick={onClose}>Close</button>
        </div>
      </div>
      <p className="group-note">Generic enterprise capabilities. Pick the few that match what your repositories do; a short description makes placement more accurate.</p>
      <div className="starter-groups">
        {groups.map((group) => (
          <fieldset key={group.name} className="starter-group">
            <legend>{group.name}</legend>
            {group.capabilities.map((item) => (
              <label key={item.id} className="work-check starter-item">
                <input type="checkbox" checked={picked.has(item.id)} onChange={(event) => toggle(item.id, event.target.checked)} />
                <span><strong>{item.name}</strong><small>{item.description}</small></span>
              </label>
            ))}
          </fieldset>
        ))}
        {groups.length === 0 && <p className="group-note">No starter capabilities match.</p>}
      </div>
    </section>
  );
}

/* ---------- inline editor: the same lanes, with fields ---------- */

function MapEditor({ document, onSave, onCancel, busy }: {
  document: MapDocument; onSave: (title: string, stages: Stage[]) => Promise<void>; onCancel: () => void; busy: boolean;
}) {
  const [draft, setDraft] = useState({ title: document.title, stages: document.stages });
  const [starterOpen, setStarterOpen] = useState(false);
  const [over, setOver] = useState<string | null>(null);
  const writeStages = (stages: Stage[]) => setDraft((old) => ({ ...old, stages }));
  const setStage = (id: string, change: (item: Stage) => Stage) => setDraft((old) => ({
    ...old, stages: old.stages.map((item) => (item.id === id ? change(item) : item)),
  }));
  const taken = useMemo(() => new Set(draft.stages.flatMap((row) => row.capabilities.map((cap) => cap.id))), [draft.stages]);

  function updateCapability(stageId: string, item: Capability) {
    setStage(stageId, (row) => ({ ...row, capabilities: row.capabilities.map((cap) => (cap.id === item.id ? item : cap)) }));
  }
  function moveCapability(capId: string, to: string) {
    const from = draft.stages.find((row) => row.capabilities.some((cap) => cap.id === capId));
    const cap = from?.capabilities.find((row) => row.id === capId);
    if (!from || !cap || from.id === to) return;
    writeStages(draft.stages.map((item) => item.id === from.id
      ? { ...item, capabilities: item.capabilities.filter((row) => row.id !== capId) }
      : item.id === to ? { ...item, capabilities: [...item.capabilities, cap] } : item));
  }
  function addStarter(stageId: string, items: Capability[]) {
    setStage(stageId, (row) => ({ ...row, capabilities: [...row.capabilities, ...items.filter((item) => !taken.has(item.id))] }));
  }

  return (
    <form className="workspace-tool-form" onSubmit={(event) => { event.preventDefault(); void onSave(draft.title, draft.stages); }}>
      <div className="work-tool-panel work-tool-controls">
        <label className="field"><span>Map name</span><input value={draft.title} required maxLength={120} onChange={(event) => setDraft({ ...draft, title: event.target.value })} /></label>
        <label className="field"><span>Stage template</span>
          <select value="" onChange={(event) => { const names = templates[event.target.value]; if (names) writeStages([...draft.stages, ...names.map(newStage)]); }}>
            <option value="">Add stages from a template</option>
            {Object.keys(templates).map((template) => <option key={template} value={template}>{template}</option>)}
          </select>
        </label>
        <div className="work-tool-actions">
          <button type="button" className="btn btn-quiet" aria-expanded={starterOpen} onClick={() => {
            if (!draft.stages.length) writeStages([newStage()]);
            setStarterOpen((open) => !open);
          }}><Library size={14} aria-hidden="true" /> Starter capabilities</button>
          <button type="button" className="btn btn-quiet" onClick={onCancel}>Cancel</button>
          <button type="submit" className="btn btn-primary" disabled={busy}>Save map</button>
        </div>
      </div>
      {starterOpen && draft.stages.length > 0 && <StarterPicker key={draft.stages.map((row) => row.id).join()} stages={draft.stages} taken={taken} onAdd={addStarter} onClose={() => setStarterOpen(false)} />}

      <div className="business-lanes is-editing" aria-label="Value chain, editing">
        {draft.stages.map((item, index) => (
          <section className={`business-lane ${over === item.id ? "is-drop-target" : ""}`} key={item.id} aria-label={item.name || "Stage"}
            onDragOver={(event) => { if (event.dataTransfer.types.includes(CAPABILITY_DRAG)) { event.preventDefault(); setOver(item.id); } }}
            onDragLeave={() => setOver((current) => (current === item.id ? null : current))}
            onDrop={(event) => { event.preventDefault(); setOver(null); moveCapability(event.dataTransfer.getData(CAPABILITY_DRAG), item.id); }}>
            <div className="business-lane-head">
              <label className="field"><span>Stage name</span><input value={item.name} required maxLength={120} onChange={(e) => setStage(item.id, (row) => ({ ...row, name: e.target.value }))} /></label>
              <div className="work-tool-actions">
                <button type="button" className="icon-btn" aria-label={`Move ${item.name} earlier`} disabled={index === 0} onClick={() => writeStages(swap(draft.stages, index, -1))}><ArrowLeft size={15} aria-hidden="true" /></button>
                <button type="button" className="icon-btn" aria-label={`Move ${item.name} later`} disabled={index === draft.stages.length - 1} onClick={() => writeStages(swap(draft.stages, index, 1))}><ArrowRight size={15} aria-hidden="true" /></button>
                <button type="button" className="icon-btn" aria-label={`Remove ${item.name} stage`} onClick={() => writeStages(draft.stages.filter((row) => row.id !== item.id))}><Trash2 size={15} aria-hidden="true" /></button>
              </div>
            </div>
            {item.capabilities.map((cap) => (
              <div key={cap.id} className="business-capability is-editing" draggable
                onDragStart={(event) => { event.dataTransfer.setData(CAPABILITY_DRAG, cap.id); event.dataTransfer.effectAllowed = "move"; }}>
                <div className="business-capability-grip"><GripVertical size={14} aria-hidden="true" />
                  <button type="button" className="icon-btn" aria-label={`Remove ${cap.name} capability`} onClick={() => setStage(item.id, (row) => ({ ...row, capabilities: row.capabilities.filter((c) => c.id !== cap.id) }))}><X size={14} aria-hidden="true" /></button>
                </div>
                <label className="field"><span>Capability name</span><input value={cap.name} required maxLength={120} onChange={(e) => updateCapability(item.id, { ...cap, name: e.target.value })} /></label>
                <label className="field"><span>Description</span><textarea rows={2} value={cap.description} maxLength={500} placeholder="What this capability does, in a sentence" onChange={(e) => updateCapability(item.id, { ...cap, description: e.target.value })} /></label>
                {draft.stages.length > 1 && <label className="field"><span>Stage</span>
                  <select value={item.id} onChange={(e) => moveCapability(cap.id, e.target.value)}>{draft.stages.map((row) => <option key={row.id} value={row.id}>{row.name}</option>)}</select>
                </label>}
              </div>
            ))}
            <button type="button" className="btn btn-quiet btn-small" onClick={() => setStage(item.id, (row) => ({ ...row, capabilities: [...row.capabilities, { id: crypto.randomUUID(), name: "New capability", description: "" }] }))}>
              <Plus size={14} aria-hidden="true" /> Add capability
            </button>
          </section>
        ))}
        <button type="button" className="business-lane business-lane-add" onClick={() => writeStages([...draft.stages, newStage()])}>
          <Plus size={16} aria-hidden="true" /> Add stage
        </button>
      </div>
    </form>
  );
}

/* ---------- a component on the map ---------- */

function Subject({ subject, placement, capabilities, busy, compact, onReview, onOpen }: {
  subject: MapSubject; placement?: Placement; capabilities: Capability[]; busy: boolean; compact: boolean; onReview: Review; onOpen: OpenEntity;
}) {
  const [target, setTarget] = useState("");
  const byId = useMemo(() => new Map(capabilities.map((cap) => [cap.id, cap.name])), [capabilities]);
  const assigned = Boolean(placement && isPlaced(placement));
  const proposed = !assigned && placement?.capability_id ? byId.get(placement.capability_id) : undefined;
  const candidates = !assigned ? (placement?.candidates ?? []).filter((id) => byId.has(id) && id !== placement?.capability_id).slice(0, 3) : [];
  const reason = !assigned ? placementReason(placement) : null;

  const controls = (
    <div className="business-subject-controls">
      {placement?.capability_id && placement.status !== "confirmed" && <button type="button" className="btn btn-quiet btn-small" disabled={busy} onClick={() => void onReview(subject.lineage_id, "confirm")}><Check size={13} aria-hidden="true" /> Confirm</button>}
      {placement?.capability_id && <button type="button" className="btn btn-quiet btn-small" disabled={busy} onClick={() => void onReview(subject.lineage_id, assigned ? "remove" : "reject")}>{assigned ? "Remove" : "Reject"}</button>}
      <div className="business-subject-move">
        <select aria-label={`Capability for ${subject.name}`} value={target} onChange={(event) => setTarget(event.target.value)}>
          <option value="">Choose capability</option>
          {capabilities.map((cap) => <option key={cap.id} value={cap.id}>{cap.name}</option>)}
        </select>
        <button type="button" className="btn btn-quiet btn-small" disabled={busy || !target} onClick={() => void onReview(subject.lineage_id, "move", target)}>{assigned ? "Move" : "Place"}</button>
      </div>
    </div>
  );

  return (
    <li className={`business-subject ${compact ? "is-compact" : ""} ${placement?.status === "ai" ? "is-ai" : ""}`} draggable={!busy} onDragStart={(event) => {
      event.dataTransfer.setData(SUBJECT_DRAG, subject.lineage_id);
      event.dataTransfer.effectAllowed = "move";
    }}>
      <div className="business-subject-meta">
        <button type="button" className="link-btn" onClick={() => onOpen(subject.entity_id, subject.rkp_hash)}>{subject.name}</button>
        <span>{subject.repository_name} · {(subject.component_kind ?? subject.kind).replaceAll("_", " ")}</span>
      </div>
      {placement && <Badge tone={placement.status === "confirmed" ? "verified" : placement.status === "ai" ? "inferred" : "neutral"}>{placementLabel(placement)}</Badge>}
      {proposed && <p className="group-note">Proposed: {proposed}</p>}
      {reason && <p className="group-note">{reason}</p>}
      {placement?.status === "declared" && placement.evidence_ids.slice(0, 2).map((id) => <SourceEvidence key={id} hash={subject.rkp_hash} id={id} />)}
      {candidates.length > 0 && (
        <div className="business-candidates">
          <span>Closest by wording</span>
          {candidates.map((id) => (
            <button key={id} type="button" className="chip" disabled={busy} onClick={() => void onReview(subject.lineage_id, "move", id)} aria-label={`Place ${subject.name} in ${byId.get(id)}`}>{byId.get(id)}</button>
          ))}
        </div>
      )}
      {compact ? <details className="business-subject-change"><summary>Change</summary>{controls}</details> : controls}
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

/* ---------- dependency arrows drawn over the lanes ---------- */

interface Arrow { key: string; d: string; candidate: boolean }

function useArrows(container: RefObject<HTMLDivElement | null>, edges: WorkspaceMap["dependencies"], revision: unknown) {
  const [state, setState] = useState<{ arrows: Arrow[]; width: number; height: number }>({ arrows: [], width: 0, height: 0 });
  const measure = useCallback(() => {
    const root = container.current;
    if (!root) return;
    const origin = root.getBoundingClientRect();
    const box = (id: string) => {
      const node = root.querySelector<HTMLElement>(`[data-capability="${CSS.escape(id)}"]`);
      if (!node) return null;
      const r = node.getBoundingClientRect();
      const left = r.left - origin.left + root.scrollLeft;
      const top = r.top - origin.top + root.scrollTop;
      return { left, right: left + r.width, y: top + 20 };
    };
    const arrows: Arrow[] = [];
    edges.forEach((edge, index) => {
      const a = box(edge.from), b = box(edge.to);
      if (!a || !b) return;
      const offset = (index % 4) * 5; // keep parallel arrows from sitting on top of each other
      let d: string;
      if (b.left >= a.right) {
        const mid = (b.left - a.right) / 2;
        d = `M ${a.right} ${a.y + offset} C ${a.right + mid} ${a.y + offset}, ${b.left - mid} ${b.y + offset}, ${b.left - 3} ${b.y + offset}`;
      } else if (b.right <= a.left) {
        const mid = (a.left - b.right) / 2;
        d = `M ${a.left} ${a.y + offset} C ${a.left - mid} ${a.y + offset}, ${b.right + mid} ${b.y + offset}, ${b.right + 3} ${b.y + offset}`;
      } else {
        const out = Math.max(a.right, b.right) + 14 + offset;
        d = `M ${a.right} ${a.y} C ${out} ${a.y}, ${out} ${b.y}, ${b.right + 3} ${b.y}`;
      }
      arrows.push({ key: `${edge.from}:${edge.to}`, d, candidate: edge.resolution !== "exact" });
    });
    const next = { arrows, width: root.scrollWidth, height: root.scrollHeight };
    setState((old) => (JSON.stringify(old) === JSON.stringify(next) ? old : next));
  }, [container, edges]);

  useLayoutEffect(() => { measure(); }, [measure, revision]);
  useEffect(() => {
    const root = container.current;
    if (!root || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => measure());
    observer.observe(root);
    root.querySelectorAll("[data-capability]").forEach((node) => observer.observe(node));
    return () => observer.disconnect();
  }, [container, measure, revision]);
  return state;
}

/* ---------- free-form canvas ---------- */

const COLUMN = 300; // width of one stage column on the canvas
const CARD = 264;   // width of a capability card on the canvas
const NUDGE = 16;   // keyboard step

type Point = { x: number; y: number };
type Arrange = (id: string, at: Point, stageId: string) => void;

function stageAt(stages: Stage[], x: number): string {
  const index = Math.min(stages.length - 1, Math.max(0, Math.floor((x + CARD / 2) / COLUMN)));
  return stages[index].id;
}

/** A capability card that can be dragged anywhere on the canvas, or nudged with the arrow keys. */
function CanvasCard({ cap, stages, canvas, at, busy, onArrange, onMoving, children }: {
  cap: Capability; stages: Stage[]; canvas: RefObject<HTMLDivElement | null>; at: Point | null; busy: boolean; onArrange: Arrange; onMoving: () => void;
  children: (grip: ReactNode, style: CSSProperties | undefined) => ReactNode;
}) {
  const [moving, setMoving] = useState<Point | null>(null);
  const start = useRef<{ pointer: Point; card: Point } | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  useEffect(() => () => clearTimeout(timer.current), []);
  const shown = moving ?? at;

  function origin(handle: HTMLElement): Point {
    if (at) return at;
    const root = canvas.current!.getBoundingClientRect();
    const box = handle.closest<HTMLElement>("[data-capability]")!.getBoundingClientRect();
    return { x: Math.round(box.left - root.left), y: Math.round(box.top - root.top) };
  }
  const clamp = (p: Point): Point => ({ x: Math.max(0, Math.min(stages.length * COLUMN - CARD, Math.round(p.x))), y: Math.max(0, Math.round(p.y)) });
  const commit = (p: Point) => onArrange(cap.id, p, stageAt(stages, p.x));

  const grip = (
    <button type="button" className="canvas-grip" aria-label={`Move ${cap.name}`} disabled={busy}
      aria-description="Drag, or use the arrow keys, to move this capability. Dropping it in another column moves it to that stage."
      onPointerDown={(event) => {
        if (event.button !== 0) return;
        event.currentTarget.setPointerCapture(event.pointerId);
        const card = origin(event.currentTarget);
        start.current = { pointer: { x: event.clientX, y: event.clientY }, card };
        setMoving(card);
      }}
      onPointerMove={(event) => {
        const from = start.current;
        if (!from) return;
        setMoving(clamp({ x: from.card.x + event.clientX - from.pointer.x, y: from.card.y + event.clientY - from.pointer.y }));
        onMoving();
      }}
      onPointerUp={() => {
        const from = start.current;
        start.current = null;
        if (from && moving && (moving.x !== from.card.x || moving.y !== from.card.y || !at)) commit(moving);
        else setMoving(null);
      }}
      onPointerCancel={() => { start.current = null; setMoving(null); }}
      onKeyDown={(event) => {
        const step = { ArrowLeft: [-NUDGE, 0], ArrowRight: [NUDGE, 0], ArrowUp: [0, -NUDGE], ArrowDown: [0, NUDGE] }[event.key];
        if (!step) return;
        event.preventDefault();
        const next = clamp({ x: (shown ?? origin(event.currentTarget)).x + step[0], y: (shown ?? origin(event.currentTarget)).y + step[1] });
        setMoving(next);
        onMoving();
        clearTimeout(timer.current);
        timer.current = setTimeout(() => commit(next), 600); // one revision per pause, not per key press
      }}>
      <Move size={14} aria-hidden="true" />
    </button>
  );
  // Keep the dragged position on screen until the saved map comes back with it.
  useEffect(() => { if (moving && at && at.x === moving.x && at.y === moving.y && !start.current) setMoving(null); }, [at, moving]);
  return <>{children(grip, shown ? { position: "absolute", left: shown.x, top: shown.y, width: CARD } : undefined)}</>;
}

/* ---------- the view ---------- */

export function BusinessMapView({ onOpen, onOpenSettings }: { onOpen: OpenEntity; onOpenSettings: () => void }) {
  const source = useApi<WorkspaceMap>("business-maps");
  const revisions = useApi<{ revisions: { version: number; author: string; updated_at: string }[] }>("business-maps/revisions");
  const [response, setResponse] = useState<WorkspaceMap>();
  const [editing, setEditing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  const [ran, setRan] = useState<{ placed: number; left: number; already: number } | null>(null);
  const [showPins, setShowPins] = useState(false);
  const [showExcluded, setShowExcluded] = useState(false);
  const [over, setOver] = useState<string | null>(null);
  const lanes = useRef<HTMLDivElement>(null);
  const data = response ?? source.data;
  const byPlacement = useMemo(() => new Map(data?.map.placements.map((row) => [row.lineage_id, row])), [data]);
  const names = useMemo(() => new Map(data?.map.stages.flatMap((row) => row.capabilities.map((cap) => [cap.id, cap.name] as const))), [data]);
  const edges = data?.dependencies ?? NO_EDGES;
  const [moved, setMoved] = useState(0); // re-measures arrows while a card is dragged
  const revision = useMemo(() => [data, moved], [data, moved]);
  const drawn = useArrows(lanes, editing ? NO_EDGES : edges, revision);

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
  const blocked = placementBlocked(data.map.placements.filter((row) => active.has(row.lineage_id)));
  const placedCount = data.subjects.filter((subject) => { const row = byPlacement.get(subject.lineage_id); return row && isPlaced(row); }).length;

  async function request(path: string, body: unknown): Promise<WorkspaceMap | null> {
    setBusy(true);
    setError(undefined);
    try {
      const next = await api<WorkspaceMap>(path, { body });
      setResponse(next);
      revisions.reload();
      return next;
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Business map could not be saved.");
      return null;
    } finally {
      setBusy(false);
    }
  }
  const review: Review = async (lineage, action, capability) => {
    setRan(null);
    await request("business-maps/placements", { expected_version: data.map.version, lineage_id: lineage, action, capability_id: capability });
  };
  async function runPlacement() {
    const before = placedCount;
    const next = await request("business-maps/run-placement", {});
    if (!next) return;
    const rows = new Map(next.map.placements.map((row) => [row.lineage_id, row]));
    const placed = next.subjects.filter((subject) => { const row = rows.get(subject.lineage_id); return row && isPlaced(row); }).length;
    const left = next.subjects.filter((subject) => { const row = rows.get(subject.lineage_id); return next.eligible_kinds.includes(subject.component_kind ?? "") && !(row && isPlaced(row)); }).length;
    const byAi = next.subjects.filter((subject) => rows.get(subject.lineage_id)?.status === "ai").length;
    const added = Math.max(0, placed - before);
    // Saving the map or settings also runs placement in the background, so a click may find the work already done.
    setRan({ placed: added, left, already: Math.max(0, byAi - added) });
  }
  function drop(event: DragEvent, capability: string) {
    event.preventDefault();
    setOver(null);
    const id = event.dataTransfer.getData(SUBJECT_DRAG);
    if (!busy && active.has(id)) void review(id, "move", capability);
  }
  const canvas = data.map.layout === "canvas";
  const arrange = (layout: "lanes" | "canvas", positions: Record<string, Point & { stage_id?: string }> = {}) =>
    request("business-maps/layout", { expected_version: data.map.version, layout, positions });
  const place: Arrange = (id, at, stageId) => { setRan(null); void arrange("canvas", { [id]: { ...at, stage_id: stageId } }); };
  const placedBottom = Math.max(0, ...capabilities.map((cap) => (cap.y ?? 0)));
  const onMoving = () => setMoved((n) => n + 1);

  function card(cap: Capability, grip?: ReactNode, style?: CSSProperties) {
    const subjects = data!.subjects.filter((subject) => {
      const row = byPlacement.get(subject.lineage_id);
      return row?.capability_id === cap.id && isPlaced(row);
    });
    const technical = [...new Set(subjects.flatMap((subject) => subject.technical_capabilities))].sort();
    const outgoing = data!.dependencies.filter((row) => row.from === cap.id);
    return <section key={cap.id} id={`capability-${cap.id}`} data-capability={cap.id} style={style} className={`business-capability ${over === cap.id ? "is-drop-target" : ""} ${grip ? "is-canvas" : ""}`} aria-label={cap.name}
      onDragOver={(event) => { if (event.dataTransfer.types.includes(SUBJECT_DRAG)) { event.preventDefault(); setOver(cap.id); } }}
      onDragLeave={() => setOver((current) => (current === cap.id ? null : current))}
      onDrop={(event) => drop(event, cap.id)}>
      {grip ? <div className="canvas-card-head"><h4>{cap.name}</h4>{grip}</div> : <h4>{cap.name}</h4>}
      <p className={cap.description ? "" : "group-note"}>{cap.description || "No description. Placement uses the name alone."}</p>
      {technical.length > 0 && <p className="business-tech">{technical.map((name) => <span key={name}>{name}</span>)}</p>}
      {outgoing.length > 0 && <p className="business-depends">Depends on {outgoing.map((edge, i) => <span key={edge.to}>{i > 0 && ", "}<a href={`#capability-${edge.to}`}>{names.get(edge.to)}</a>{edge.resolution !== "exact" && <em> (candidate)</em>}</span>)}</p>}
      {subjects.length > 0
        ? <ul className="business-subjects">{subjects.map((subject) => <Subject key={subject.lineage_id} compact subject={subject} placement={byPlacement.get(subject.lineage_id)} capabilities={capabilities} busy={busy} onReview={review} onOpen={onOpen} />)}</ul>
        : <p className="business-empty">Drop a component here</p>}
      <details className="business-cap-id"><summary>Capability ID</summary><code>{cap.id}</code></details>
    </section>;
  }
  const arrows = drawn.arrows.length > 0 && (
    <svg className="business-arrows" width={drawn.width} height={drawn.height} aria-hidden="true">
      <defs>
        <marker id="rkp-arrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0 L8 4 L0 8 z" className="business-arrow-head" /></marker>
        <marker id="rkp-arrow-candidate" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0 L8 4 L0 8 z" className="business-arrow-head is-candidate" /></marker>
      </defs>
      {drawn.arrows.map((arrow) => <path key={arrow.key} d={arrow.d} className={`business-arrow ${arrow.candidate ? "is-candidate" : ""}`} markerEnd={`url(#${arrow.candidate ? "rkp-arrow-candidate" : "rkp-arrow"})`} />)}
    </svg>
  );

  return (
    <section className="work-tool">
      <header className="work-tool-header">
        <div><h2>{data.map.title}</h2><p>Group repository components by the capabilities they support.</p></div>
        <div className="work-tool-actions">
          {!editing && data.map.version > 0 && capabilities.length > 0 && <div className="layout-switch" role="group" aria-label="Map layout">
            <button type="button" aria-pressed={!canvas} disabled={busy} onClick={() => { if (canvas) void arrange("lanes"); }}><Columns3 size={14} aria-hidden="true" /> Lanes</button>
            <button type="button" aria-pressed={canvas} disabled={busy} onClick={() => { if (!canvas) void arrange("canvas"); }}><LayoutDashboard size={14} aria-hidden="true" /> Canvas</button>
          </div>}
          <button type="button" className="btn btn-quiet" onClick={() => { setResponse(undefined); setRan(null); source.reload(); revisions.reload(); }} disabled={busy || editing}>Reload</button>
          {!editing && <button type="button" className="btn btn-quiet" disabled={busy || !capabilities.length} onClick={() => void runPlacement()}><Sparkles size={14} aria-hidden="true" /> Run placement</button>}
          {!editing && <button type="button" className="btn btn-primary" disabled={busy} onClick={() => setEditing(true)}>{data.map.version ? "Edit map" : "Create map"}</button>}
        </div>
      </header>
      {busy && <p role="status" className="note">Updating business map…</p>}
      {error && <p role="alert" className="form-error">{error}</p>}

      {!editing && capabilities.length > 0 && (blocked || ran) && (
        <div className={`placement-status ${blocked ? "is-warn" : ""}`} role="status">
          {blocked ? <>
            <strong>{blocked === "egress_denied" ? "Automatic placement is switched off." : "Automatic placement has no model to use."}</strong>
            <p>{blocked === "egress_denied"
              ? "A decision model is set, but it is not allowed to receive data."
              : "No decision model is configured for this workspace."}{" "}
              Nothing was placed by AI. Components below show their closest capabilities by wording so you can place them yourself.</p>
            <button type="button" className="link-btn" onClick={onOpenSettings}>Open decision model settings</button>
          </> : ran && <>
            <strong>Placement finished.</strong>
            <p>{ran.placed === 0
              ? (ran.already > 0 ? `Nothing new to place: ${ran.already} ${ran.already === 1 ? "component is" : "components are"} already placed by AI.` : "No components were placed.")
              : `${ran.placed} ${ran.placed === 1 ? "component was" : "components were"} placed by AI.`}{" "}
              {ran.left > 0 ? `${ran.left} still ${ran.left === 1 ? "needs" : "need"} placing; each one says why.` : "Nothing is left to place."}</p>
          </>}
        </div>
      )}

      {editing ? <MapEditor document={data.map} busy={busy} onCancel={() => setEditing(false)} onSave={async (title, stages) => {
        if (await request("business-maps", { expected_version: data.map.version, title, stages })) setEditing(false);
      }} /> : <>
        {data.map.stages.length === 0 && <p className="group-note">Create stages and capabilities to start mapping components.</p>}
        {canvas ? <>
          <p className="group-note">Drag a capability by its handle to arrange the canvas. Dropping it in another column moves it to that stage; components stay where they are placed.</p>
          <div className="business-canvas-scroll">
            <div className="business-canvas" role="region" aria-label="Value chain canvas" ref={lanes}
              style={{ width: data.map.stages.length * COLUMN, minHeight: placedBottom + 420, gridTemplateColumns: `repeat(${data.map.stages.length}, ${COLUMN}px)` }}>
              {arrows}
              {data.map.stages.map((item, index) => (
                <section className="canvas-column" key={item.id} aria-label={item.name}>
                  <h3><span className="eyebrow">{String(index + 1).padStart(2, "0")}</span>{item.name}</h3>
                  {item.capabilities.length === 0 && <p className="group-note">No capabilities yet.</p>}
                  {/* A card that has never been moved stays in its column; a moved one sits where it was put. */}
                  {item.capabilities.map((cap) => (
                    <CanvasCard key={cap.id} cap={cap} stages={data.map.stages} canvas={lanes} busy={busy} onArrange={place} onMoving={onMoving}
                      at={cap.x != null && cap.y != null ? { x: cap.x, y: cap.y } : null}>
                      {(grip, style) => card(cap, grip, style)}
                    </CanvasCard>
                  ))}
                </section>
              ))}
            </div>
          </div>
        </> : <div className="business-lanes" aria-label="Value chain" ref={lanes}>
          {arrows}
          {data.map.stages.map((item, index) => (
            <section className="business-lane" key={item.id}>
              <h3><span className="eyebrow">{String(index + 1).padStart(2, "0")}</span>{item.name}</h3>
              {item.capabilities.length === 0 && <p className="group-note">No capabilities yet.</p>}
              {item.capabilities.map((cap) => card(cap))}
            </section>
          ))}
        </div>}
        {edges.length > 0 && <p className="business-legend" aria-hidden="true"><span className="legend-line" /> depends on <span className="legend-line is-candidate" /> candidate, not fully resolved</p>}

        {capabilities.length > 0 && <section className="work-tool-panel">
          <div className="work-tool-controls"><h3>Needs placing <span className="count">{tray.length}</span></h3>
            <label className="work-check"><input type="checkbox" checked={showPins} onChange={(event) => setShowPins(event.target.checked)} /> Show endpoints and tables to pin</label>
            <label className="work-check"><input type="checkbox" checked={showExcluded} onChange={(event) => setShowExcluded(event.target.checked)} /> Show other component kinds</label>
          </div>
          {tray.length ? <ul className="business-subjects business-tray">{tray.map((subject) => <Subject key={subject.lineage_id} compact={false} subject={subject} placement={byPlacement.get(subject.lineage_id)} capabilities={capabilities} busy={busy} onReview={review} onOpen={onOpen} />)}</ul> : <p className="group-note">No subjects need review.</p>}
        </section>}
        {orphaned.length > 0 && <section className="work-tool-panel">
          <h3>Orphaned placements</h3>
          <p className="group-note">These components no longer exist in the latest snapshot of their repository.</p>
          {orphaned.map((row) => <p key={row.lineage_id}><code>{row.lineage_id}</code> · {names.get(row.capability_id ?? "")} <button type="button" className="btn btn-quiet btn-small" disabled={busy} onClick={() => void review(row.lineage_id, "remove")}>Remove</button></p>)}
        </section>}
        {data.dependencies.length > 0 && <section className="work-tool-panel">
          <h3>Capability dependencies</h3>
          <p className="group-note">Derived from relationships between the placed components. Open one to see the evidence.</p>
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
