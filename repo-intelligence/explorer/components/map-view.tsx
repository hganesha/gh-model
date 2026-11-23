"use client";

import { ArrowUpRight, FileText, Grid3x3, Minus, Plus } from "lucide-react";
import { domainLabel, kindsForPlane, PLANES, unknownDomain, type PlaneMeta } from "@/lib/planes";
import { kindLabel, measured, percent, plural } from "@/lib/format";
import type { Summary } from "@/lib/types";
import { Coverage, PlaneIcon } from "./ui";

export interface MapPrefs {
  zoom: number;
  grid: boolean;
}

export const ZOOM_STEPS = [0.8, 0.9, 1, 1.1, 1.2];

function unknownsForPlane(summary: Summary, plane: PlaneMeta): number {
  return summary.unknowns.filter((u) => {
    const d = unknownDomain(u.coverage_ref);
    return d !== null && plane.coverage.includes(d);
  }).length;
}

function PlaneCard({ plane, summary, index, onOpen }: { plane: PlaneMeta; summary: Summary; index: number; onOpen: () => void }) {
  const kinds = kindsForPlane(summary, plane);
  const total = summary.planes?.[plane.id]?.total ?? kinds.reduce((n, k) => n + k.count, 0);
  const max = Math.max(1, ...kinds.map((k) => k.count));
  const unknowns = unknownsForPlane(summary, plane);

  return (
    <button
      type="button"
      className={`plane-card plane-${plane.id} ${total === 0 ? "plane-empty" : ""}`}
      style={{ animationDelay: `${index * 45}ms` }}
      onClick={onOpen}
      aria-label={`Open ${plane.title}: ${plural(total, "entity", "entities")}`}
    >
      <div className="plane-top">
        <span className="plane-icon">
          <PlaneIcon plane={plane.id} />
        </span>
        <span className="eyebrow">{plane.eyebrow}</span>
        <ArrowUpRight className="plane-open" size={15} aria-hidden="true" />
      </div>
      <div className="plane-title">
        <h3>{plane.title}</h3>
        <span className="plane-total">{total.toLocaleString("en-US")}</span>
      </div>
      <p className="plane-blurb">{plane.blurb}</p>

      {total === 0 ? (
        <p className="plane-nothing">
          Nothing found for this plane in this snapshot. That is not proof of absence — the compiler may not recognise how this repository does it.
        </p>
      ) : (
        <ul className="kind-rows">
          {kinds.map(({ kind, count }) => (
            <li key={kind} className={count === 0 ? "kind-zero" : ""}>
              <span className="kind-name">{kindLabel(kind)}</span>
              <span className="kind-meter" aria-hidden="true">
                <i style={{ width: `${count === 0 ? 0 : Math.max(3, (Math.sqrt(count) / Math.sqrt(max)) * 100)}%` }} />
              </span>
              <span className="kind-count">{count === 0 ? "none found" : count.toLocaleString("en-US")}</span>
            </li>
          ))}
        </ul>
      )}

      <div className="plane-foot">
        {plane.coverage.length === 0 ? (
          <span className="plane-foot-quiet">No coverage measure for this plane</span>
        ) : (
          plane.coverage.map((d) => <Coverage key={d} compact label={domainLabel(d)} ratio={summary.coverage_totals?.[d]} />)
        )}
        {unknowns > 0 && <span className="unknown-chip">{plural(unknowns, "unknown")}</span>}
      </div>
    </button>
  );
}

function QualityCard({ summary, index, onOpen }: { summary: Summary; index: number; onOpen: () => void }) {
  const domains = Object.entries(summary.coverage_totals ?? {});
  const ranked = domains.filter(([, r]) => measured(r)).sort((a, b) => percent(a[1]) - percent(b[1]));
  const unmeasured = domains.filter(([, r]) => !measured(r)).length;
  return (
    <button type="button" className="plane-card plane-quality" style={{ animationDelay: `${index * 45}ms` }} onClick={onOpen} aria-label="Open Quality: coverage, unknowns and conflicts">
      <div className="plane-top">
        <span className="plane-icon">
          <PlaneIcon plane="quality" />
        </span>
        <span className="eyebrow">07 · QUALITY</span>
        <ArrowUpRight className="plane-open" size={15} aria-hidden="true" />
      </div>
      <div className="plane-title">
        <h3>Quality</h3>
      </div>
      <p className="plane-blurb">How much of the repository the compiler resolved, and what it could not.</p>
      <div className="quality-figures">
        <div>
          <b>{summary.unknowns.length.toLocaleString("en-US")}</b>
          <span>unknowns</span>
        </div>
        <div>
          <b>{summary.conflicts.length.toLocaleString("en-US")}</b>
          <span>conflicts</span>
        </div>
        <div>
          <b>{unmeasured}</b>
          <span>not measured</span>
        </div>
      </div>
      <div className="plane-foot">
        {ranked.length === 0 && <span className="plane-foot-quiet">No coverage was measured</span>}
        {ranked.slice(0, 3).map(([d, r]) => (
          <Coverage key={d} compact label={domainLabel(d)} ratio={r} />
        ))}
      </div>
    </button>
  );
}

export function MapView({
  summary,
  prefs,
  onPrefs,
  onOpenPlane,
  onOpenQuality,
}: {
  summary: Summary;
  prefs: MapPrefs;
  onPrefs: (next: MapPrefs) => void;
  onOpenPlane: (plane: string) => void;
  onOpenQuality: () => void;
}) {
  const zoomIndex = Math.max(0, ZOOM_STEPS.indexOf(prefs.zoom));
  return (
    <section className={`canvas ${prefs.grid ? "canvas-grid" : ""}`} aria-label="Repository map">
      <div className="canvas-toolbar">
        <div>
          <h2>Repository map</h2>
          <p>Six planes compiled from one commit, plus what the compiler could not resolve.</p>
        </div>
        <div className="canvas-tools">
          <button type="button" className={`icon-btn ${prefs.grid ? "is-on" : ""}`} aria-pressed={prefs.grid} aria-label="Toggle grid" onClick={() => onPrefs({ ...prefs, grid: !prefs.grid })}>
            <Grid3x3 size={15} aria-hidden="true" />
          </button>
          <span className="tool-divider" aria-hidden="true" />
          <button type="button" className="icon-btn" aria-label="Zoom out" disabled={zoomIndex <= 0} onClick={() => onPrefs({ ...prefs, zoom: ZOOM_STEPS[zoomIndex - 1] })}>
            <Minus size={15} aria-hidden="true" />
          </button>
          <span className="zoom-value" aria-live="polite">
            {Math.round(prefs.zoom * 100)}%
          </span>
          <button type="button" className="icon-btn" aria-label="Zoom in" disabled={zoomIndex >= ZOOM_STEPS.length - 1} onClick={() => onPrefs({ ...prefs, zoom: ZOOM_STEPS[zoomIndex + 1] })}>
            <Plus size={15} aria-hidden="true" />
          </button>
        </div>
      </div>

      <div className="plane-grid" style={{ zoom: prefs.zoom }}>
        {PLANES.map((plane, i) => (
          <PlaneCard key={plane.id} plane={plane} summary={summary} index={i} onOpen={() => onOpenPlane(plane.id)} />
        ))}
        <QualityCard summary={summary} index={PLANES.length} onOpen={onOpenQuality} />
      </div>

      <button type="button" className="files-strip" onClick={() => onOpenPlane("files")}>
        <FileText size={15} aria-hidden="true" />
        <span>
          <b>{plural(summary.files ?? 0, "file")}</b> in this snapshot
        </span>
        <span className="files-strip-go">
          Browse files <ArrowUpRight size={13} aria-hidden="true" />
        </span>
      </button>
    </section>
  );
}
