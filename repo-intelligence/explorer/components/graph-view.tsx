"use client";

import { forceCenter, forceCollide, forceLink, forceManyBody, forceSimulation, forceX, forceY, type SimulationLinkDatum, type SimulationNodeDatum } from "d3-force";
import { Maximize2, Minus, Plus, RotateCcw } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { snapshotPath, useApi } from "@/lib/api";
import { kindLabel, plural, truncate } from "@/lib/format";
import { planeOfKind, PLANES, PREDICATES, STRUCTURAL_PREDICATES } from "@/lib/planes";
import type { Assertion, EntitiesResponse, EntityRef, QueryResponse } from "@/lib/types";
import { ErrorNote, isTentative, Loading } from "./ui";

const MAX_NODES = 150;
const LABEL_CHARS = 16;

export interface GraphPrefs {
  depth: number;
  predicates: string[];
}

export const DEFAULT_GRAPH_PREFS: GraphPrefs = {
  depth: 2,
  predicates: PREDICATES.filter((p) => !STRUCTURAL_PREDICATES.includes(p)),
};

interface LaidOutNode extends SimulationNodeDatum {
  id: string;
  kind: string;
  name: string;
  degree: number;
  x: number;
  y: number;
}

interface LaidOutEdge {
  id: string;
  source: LaidOutNode;
  target: LaidOutNode;
  assertion: Assertion;
}

/** Run the force simulation to rest synchronously; 150 nodes settle in a few milliseconds. */
function layout(result: QueryResponse, seedIds: Set<string>): { nodes: LaidOutNode[]; edges: LaidOutEdge[] } {
  const nodes: LaidOutNode[] = result.entities.map((e) => ({ id: e.id, kind: e.kind, name: e.name, degree: 0, x: 0, y: 0 }));
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const links: (SimulationLinkDatum<LaidOutNode> & { assertion: Assertion })[] = [];
  for (const a of result.assertions) {
    const s = byId.get(a.subject);
    const t = byId.get(a.object);
    if (!s || !t || s === t) continue;
    s.degree += 1;
    t.degree += 1;
    links.push({ source: s, target: t, assertion: a });
  }
  // Deterministic start positions on a spiral so the same data always draws the same picture.
  nodes.forEach((n, i) => {
    const r = 14 * Math.sqrt(i + 0.5);
    const angle = i * 2.399963;
    n.x = r * Math.cos(angle);
    n.y = r * Math.sin(angle);
  });
  const sim = forceSimulation(nodes)
    .force("link", forceLink(links).distance(96).strength(0.5))
    .force("charge", forceManyBody().strength(-380).distanceMax(600))
    .force("collide", forceCollide<LaidOutNode>().radius((n) => nodeRadius(n, seedIds) + 30))
    .force("center", forceCenter(0, 0))
    .force("x", forceX(0).strength(0.035))
    .force("y", forceY(0).strength(0.05))
    .stop();
  const ticks = Math.min(400, 120 + nodes.length * 2);
  for (let i = 0; i < ticks; i += 1) sim.tick();
  const edges = links.map((l) => ({ id: l.assertion.id, source: l.source as LaidOutNode, target: l.target as LaidOutNode, assertion: l.assertion }));
  return { nodes, edges };
}

function nodeRadius(n: LaidOutNode, seeds: Set<string>): number {
  return seeds.has(n.id) ? 11 : Math.min(9, 5 + Math.sqrt(n.degree));
}

interface Transform {
  x: number;
  y: number;
  k: number;
}

function GraphCanvas({
  result,
  seedIds,
  selectedId,
  onSelect,
}: {
  result: QueryResponse;
  seedIds: Set<string>;
  selectedId: string | null;
  onSelect: (id: string) => void;
}) {
  const { nodes, edges } = useMemo(() => layout(result, seedIds), [result, seedIds]);
  const wrap = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({ w: 800, h: 560 });
  const [transform, setTransform] = useState<Transform | null>(null);
  const [hoverEdge, setHoverEdge] = useState<string | null>(null);
  const [hoverNode, setHoverNode] = useState<string | null>(null);
  const drag = useRef<{ x: number; y: number; tx: number; ty: number; moved: boolean } | null>(null);

  useEffect(() => {
    const el = wrap.current;
    if (!el) return;
    const measure = () => setSize({ w: Math.max(240, el.clientWidth), h: Math.max(240, el.clientHeight) });
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  const fit = useCallback((): Transform => {
    if (!nodes.length) return { x: size.w / 2, y: size.h / 2, k: 1 };
    const xs = nodes.map((n) => n.x);
    const ys = nodes.map((n) => n.y);
    const minX = Math.min(...xs) - 70;
    const maxX = Math.max(...xs) + 70;
    const minY = Math.min(...ys) - 40;
    const maxY = Math.max(...ys) + 50;
    const k = Math.min(1.4, Math.max(0.2, Math.min(size.w / (maxX - minX), size.h / (maxY - minY))));
    return { x: size.w / 2 - ((minX + maxX) / 2) * k, y: size.h / 2 - ((minY + maxY) / 2) * k, k };
  }, [nodes, size]);

  // Refit whenever the data changes; user pan/zoom is kept across resizes.
  useEffect(() => setTransform(null), [nodes]);
  const t = transform ?? fit();

  const zoomBy = useCallback(
    (factor: number, cx = size.w / 2, cy = size.h / 2) => {
      setTransform((prev) => {
        const cur = prev ?? fit();
        const k = Math.min(3, Math.max(0.15, cur.k * factor));
        const ratio = k / cur.k;
        return { k, x: cx - (cx - cur.x) * ratio, y: cy - (cy - cur.y) * ratio };
      });
    },
    [fit, size],
  );

  // Wheel must be registered non-passive so the page does not scroll while zooming.
  useEffect(() => {
    const el = wrap.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const rect = el.getBoundingClientRect();
      zoomBy(Math.exp(-e.deltaY * 0.0015), e.clientX - rect.left, e.clientY - rect.top);
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, [zoomBy]);

  function onPointerDown(e: React.PointerEvent) {
    if ((e.target as Element).closest("[data-node]")) return;
    drag.current = { x: e.clientX, y: e.clientY, tx: t.x, ty: t.y, moved: false };
    (e.currentTarget as Element).setPointerCapture(e.pointerId);
  }
  function onPointerMove(e: React.PointerEvent) {
    const d = drag.current;
    if (!d) return;
    const dx = e.clientX - d.x;
    const dy = e.clientY - d.y;
    if (Math.abs(dx) + Math.abs(dy) > 2) d.moved = true;
    setTransform({ k: t.k, x: d.tx + dx, y: d.ty + dy });
  }
  function onPointerUp() {
    drag.current = null;
  }

  const focusId = hoverNode ?? selectedId;
  // Labels keep a readable on-screen size while zoomed out; below that they shrink with the graph.
  const labelSize = 9.5 / Math.min(1, Math.max(t.k, 0.5));

  return (
    <div className="graph-canvas" ref={wrap}>
      <svg
        width={size.w}
        height={size.h}
        viewBox={`0 0 ${size.w} ${size.h}`}
        role="group"
        aria-label={`Relationship graph with ${nodes.length} entities and ${edges.length} relationships`}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={onPointerUp}
      >
        <defs>
          <marker id="arrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
            <path d="M0 0.8 L7 4 L0 7.2 Z" className="edge-arrow" />
          </marker>
        </defs>
        <g transform={`translate(${t.x} ${t.y}) scale(${t.k})`}>
          <g>
            {edges.map((e) => {
              const dx = e.target.x - e.source.x;
              const dy = e.target.y - e.source.y;
              const len = Math.hypot(dx, dy) || 1;
              const rs = nodeRadius(e.source, seedIds) + 2;
              const rt = nodeRadius(e.target, seedIds) + 4;
              const x1 = e.source.x + (dx / len) * rs;
              const y1 = e.source.y + (dy / len) * rs;
              const x2 = e.target.x - (dx / len) * rt;
              const y2 = e.target.y - (dy / len) * rt;
              const active = hoverEdge === e.id || (focusId !== null && (e.source.id === focusId || e.target.id === focusId));
              const tentative = isTentative(e.assertion);
              return (
                <g key={e.id} className={`edge ${tentative ? "edge-tentative" : ""} ${active ? "is-active" : ""}`} onPointerEnter={() => setHoverEdge(e.id)} onPointerLeave={() => setHoverEdge(null)}>
                  <line x1={x1} y1={y1} x2={x2} y2={y2} className="edge-hit" />
                  <line x1={x1} y1={y1} x2={x2} y2={y2} className="edge-line" markerEnd="url(#arrow)" />
                  {active && (
                    <text x={(x1 + x2) / 2} y={(y1 + y2) / 2 - 4} className="edge-label" textAnchor="middle" style={{ fontSize: 9.5 / Math.max(0.6, t.k) }}>
                      {e.assertion.predicate}
                      {e.assertion.resolution === "candidate" ? " · candidate" : e.assertion.epistemic_status === "inferred" ? " · inferred" : ""}
                    </text>
                  )}
                </g>
              );
            })}
          </g>
          <g>
            {nodes.map((n) => {
              const r = nodeRadius(n, seedIds);
              const isSeed = seedIds.has(n.id);
              const selected = n.id === selectedId;
              return (
                <g
                  key={n.id}
                  data-node
                  className={`node plane-${planeOfKind(n.kind)} ${isSeed ? "is-seed" : ""} ${selected ? "is-selected" : ""}`}
                  transform={`translate(${n.x} ${n.y})`}
                  role="button"
                  tabIndex={0}
                  aria-label={`${kindLabel(n.kind)} ${n.name}${isSeed ? ", seed" : ""}`}
                  onClick={() => onSelect(n.id)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault();
                      onSelect(n.id);
                    }
                  }}
                  onPointerEnter={() => setHoverNode(n.id)}
                  onPointerLeave={() => setHoverNode(null)}
                >
                  <title>{`${n.name} — ${kindLabel(n.kind)}`}</title>
                  {(isSeed || selected) && <circle r={r + 4} className="node-ring" />}
                  <circle r={r} className="node-dot" />
                  <text y={r + 2 + labelSize} textAnchor="middle" className="node-label" style={{ fontSize: isSeed || selected ? labelSize * 1.1 : labelSize }}>
                    {truncate(n.name, LABEL_CHARS)}
                  </text>
                </g>
              );
            })}
          </g>
        </g>
      </svg>
      <div className="graph-zoom">
        <button type="button" className="icon-btn" aria-label="Zoom in" onClick={() => zoomBy(1.25)}>
          <Plus size={15} aria-hidden="true" />
        </button>
        <button type="button" className="icon-btn" aria-label="Zoom out" onClick={() => zoomBy(0.8)}>
          <Minus size={15} aria-hidden="true" />
        </button>
        <button type="button" className="icon-btn" aria-label="Fit graph to view" onClick={() => setTransform(null)}>
          <Maximize2 size={14} aria-hidden="true" />
        </button>
      </div>
    </div>
  );
}

export function GraphView({
  hash,
  seed,
  prefs,
  onPrefs,
  onResetSeed,
  onOpenEntity,
  selectedId,
}: {
  hash: string;
  seed: EntityRef | null;
  prefs: GraphPrefs;
  onPrefs: (next: GraphPrefs) => void;
  onResetSeed: () => void;
  onOpenEntity: (id: string) => void;
  selectedId: string | null;
}) {
  // Default seed: the Repository entity.
  const repoQuery = useApi<EntitiesResponse>(seed ? null : snapshotPath(hash, "entities?plane=architecture&kind=Repository&limit=1"));
  const repoEntity = repoQuery.data?.entities[0] ?? null;
  const structuralHidden = !prefs.predicates.includes("CONTAINS");
  // The repository reaches everything else only through CONTAINS. When that is hidden,
  // the overview also starts from the repository's components so it is not a lone dot.
  const widen = !seed && structuralHidden;
  const componentQuery = useApi<EntitiesResponse>(widen ? snapshotPath(hash, "entities?plane=architecture&kind=Component&limit=40") : null);

  const primary: EntityRef | null = seed ?? (repoEntity ? { id: repoEntity.id, kind: repoEntity.kind, name: repoEntity.name } : null);
  const seedsReady = primary !== null && (!widen || componentQuery.data !== undefined || componentQuery.error !== undefined);
  const extraSeeds = widen ? (componentQuery.data?.entities ?? []).map((e) => e.id) : [];
  const seedList = primary ? [primary.id, ...extraSeeds] : [];
  const seedKey = seedList.join(",");
  const seedIds = useMemo(() => new Set(seedKey ? seedKey.split(",") : []), [seedKey]);

  const nothingSelected = prefs.predicates.length === 0;
  const query = useApi<QueryResponse>(
    seedsReady && !nothingSelected ? "knowledge/query" : null,
    seedsReady ? { rkp_hash: hash, seed_entities: seedList, allowed_edge_types: prefs.predicates, max_depth: prefs.depth, max_nodes: MAX_NODES } : undefined,
  );

  function toggle(predicate: string) {
    const on = prefs.predicates.includes(predicate);
    onPrefs({ ...prefs, predicates: on ? prefs.predicates.filter((p) => p !== predicate) : PREDICATES.filter((p) => p === predicate || prefs.predicates.includes(p)) });
  }

  const result = query.data;
  const tentativeCount = result ? result.assertions.filter(isTentative).length : 0;

  return (
    <section className="graph-view" aria-label="Graph">
      <div className="graph-controls">
        <div className="graph-seed">
          <span className="eyebrow">NEIGHBOURHOOD OF</span>
          <strong>{primary ? primary.name : repoQuery.loading ? "…" : "no seed"}</strong>
          {primary && <span className="neighbour-kind">{kindLabel(primary.kind)}</span>}
          {seed && (
            <button type="button" className="btn btn-quiet btn-small" onClick={onResetSeed}>
              <RotateCcw size={12} aria-hidden="true" /> Back to repository
            </button>
          )}
        </div>
        <div className="depth" role="group" aria-label="Traversal depth">
          <span>Depth</span>
          {[1, 2, 3].map((d) => (
            <button type="button" key={d} className={`seg ${prefs.depth === d ? "is-on" : ""}`} aria-pressed={prefs.depth === d} onClick={() => onPrefs({ ...prefs, depth: d })}>
              {d}
            </button>
          ))}
        </div>
        <div className="chips predicate-chips" role="group" aria-label="Relationship types to follow">
          {PREDICATES.map((p) => {
            const on = prefs.predicates.includes(p);
            return (
              <button type="button" key={p} className={`chip chip-mono ${on ? "is-on" : ""}`} aria-pressed={on} onClick={() => toggle(p)}>
                {p}
              </button>
            );
          })}
        </div>
      </div>

      <div className="graph-notices" aria-live="polite">
        {result?.truncated && (
          <p className="notice notice-warn">
            Truncated: showing {result.entities.length} nodes (limit {MAX_NODES}). Lower the depth or follow fewer relationship types to see a complete neighbourhood.
          </p>
        )}
        {widen && extraSeeds.length > 0 && (
          <p className="notice">
            CONTAINS is hidden, so this overview also starts from the {plural(extraSeeds.length, "component")} of the repository. Turn CONTAINS on to walk the hierarchy itself.
          </p>
        )}
        {result && result.entities.length <= seedList.length && result.assertions.length === 0 && (
          <p className="notice">
            No relationships of the selected types reach this entity within depth {prefs.depth}. That only describes this snapshot and these filters
            {structuralHidden ? " — CONTAINS and DEFINED_IN are currently hidden." : "."}
          </p>
        )}
      </div>

      <div className="graph-stage">
        {repoQuery.error && <ErrorNote error={repoQuery.error} onRetry={repoQuery.reload} what="the repository entity" />}
        {!seed && repoQuery.data && !repoEntity && <p className="state">This snapshot has no Repository entity to start from. Open an entity and choose “View in graph”.</p>}
        {nothingSelected && <p className="state">Select at least one relationship type to draw a graph.</p>}
        {(repoQuery.loading || (widen && componentQuery.loading) || query.loading) && <Loading label="Laying out graph" />}
        {query.error && <ErrorNote error={query.error} onRetry={query.reload} what="the graph" />}
        {result && <GraphCanvas result={result} seedIds={seedIds} selectedId={selectedId} onSelect={onOpenEntity} />}
      </div>

      <footer className="graph-legend">
        <ul>
          {[...PLANES.map((p) => ({ id: p.id as string, label: p.title })), { id: "files", label: "Files" }].map((p) => (
            <li key={p.id}>
              <i className={`dot plane-${p.id}`} aria-hidden="true" /> {p.label}
            </li>
          ))}
        </ul>
        <ul>
          <li>
            <svg width="30" height="8" aria-hidden="true">
              <line x1="0" y1="4" x2="30" y2="4" className="legend-solid" />
            </svg>
            exact, supported
          </li>
          <li>
            <svg width="30" height="8" aria-hidden="true">
              <line x1="0" y1="4" x2="30" y2="4" className="legend-dashed" />
            </svg>
            candidate or inferred{result ? ` (${tentativeCount})` : ""}
          </li>
          {result && (
            <li className="legend-count">
              {plural(result.entities.length, "node")} · {plural(result.assertions.length, "edge")}
            </li>
          )}
        </ul>
      </footer>
    </section>
  );
}
