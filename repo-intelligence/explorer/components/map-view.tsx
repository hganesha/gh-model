"use client";

import { ArrowUpRight, FileText, Grid3x3, Minus, Plus } from "lucide-react";
import { domainLabel, kindsForPlane, PLANES, QUALITY_EYEBROW, unknownDomain, type PlaneMeta } from "@/lib/planes";
import { planeMeasures } from "@/lib/plane-coverage";
import { platformCoverage, type PlatformCoverage } from "@/lib/platforms";
import { kindLabel, measured, percent, plural } from "@/lib/format";
import type { Summary } from "@/lib/types";
import { PlaneMeasures, securityFound } from "./plane-coverage";
import { Coverage, PlaneIcon } from "./ui";

/** Business capabilities this snapshot's components are placed under. */
export interface Purpose {
  capabilities: { name: string; components: number; ai: number }[];
  placed: number;
  components: number;
}

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
  const hasMeasures = planeMeasures(summary, plane).length > 0;

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
        {hasMeasures ? <PlaneMeasures summary={summary} plane={plane} compact /> : plane.id !== "security" && <span className="plane-foot-quiet">No coverage measure for this plane</span>}
        {plane.id === "security" && <span className="plane-foot-quiet">{securityFound(summary)}</span>}
        {unknowns > 0 && <span className="unknown-chip">{plural(unknowns, "unknown")}</span>}
      </div>
    </button>
  );
}

function PurposeCard({ purpose, onOpen }: { purpose: Purpose; onOpen: () => void }) {
  const max = Math.max(1, ...purpose.capabilities.map((row) => row.components));
  const ai = purpose.capabilities.reduce((n, row) => n + row.ai, 0);
  return (
    <button type="button" className="plane-card plane-purpose" onClick={onOpen} aria-label={`Open business map: ${plural(purpose.capabilities.length, "capability", "capabilities")}`}>
      <div className="plane-top">
        <span className="plane-icon">
          <PlaneIcon plane="purpose" />
        </span>
        <span className="eyebrow">00 · PURPOSE</span>
        <ArrowUpRight className="plane-open" size={15} aria-hidden="true" />
      </div>
      <div className="plane-title">
        <h3>Purpose</h3>
        <span className="plane-total">{purpose.capabilities.length.toLocaleString("en-US")}</span>
      </div>
      <p className="plane-blurb">Business capabilities this repository's components are placed under on the business map.</p>
      <ul className="kind-rows">
        {purpose.capabilities.map((row) => (
          <li key={row.name}>
            <span className="kind-name">{row.name}</span>
            <span className="kind-meter" aria-hidden="true">
              <i style={{ width: `${Math.max(3, (row.components / max) * 100)}%` }} />
            </span>
            <span className="kind-count">{row.components.toLocaleString("en-US")}</span>
          </li>
        ))}
      </ul>
      <div className="plane-foot">
        <Coverage compact label="Components placed" ratio={{ numerator: purpose.placed, denominator: purpose.components }} />
        {ai > 0 && <span className="unknown-chip">{ai} placed by AI, not confirmed</span>}
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
        <span className="eyebrow">{QUALITY_EYEBROW}</span>
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

const CONSTRUCTS_SHOWN = 3;
const NOT_SEEN_SHOWN = 6;

function PlatformCard({ platform, onOpen }: { platform: PlatformCoverage; onOpen: () => void }) {
  const gaps = platform.constructs.filter((c) => c.modelled < c.seen).slice(0, CONSTRUCTS_SHOWN);
  const notSeen = platform.notSeen.slice(0, NOT_SEEN_SHOWN);
  return (
    <button type="button" className="platform-card" onClick={onOpen} aria-label={`Open Quality: ${platform.platform} constructs, ${platform.modelled} of ${platform.seen} occurrences modelled`}>
      <div className="platform-card-head">
        <code>{platform.platform}</code>
        <span>{plural(platform.constructs.length, "construct")} seen</span>
      </div>
      <Coverage compact label="Occurrences modelled" ratio={{ numerator: platform.modelled, denominator: platform.seen }} />
      {gaps.length > 0 ? (
        <ul className="platform-gaps" aria-label="Constructs seen but not fully modelled">
          {gaps.map((c) => (
            <li key={c.id}>
              <code>{c.id}</code>
              <span>
                {c.modelled.toLocaleString("en-US")} of {c.seen.toLocaleString("en-US")}
              </span>
            </li>
          ))}
        </ul>
      ) : (
        <p className="platform-note">Every construct seen was modelled.</p>
      )}
      {notSeen.length > 0 && (
        <p className="platform-note">
          Looked for, not seen: {notSeen.join(", ")}
          {platform.notSeen.length > notSeen.length ? ` and ${platform.notSeen.length - notSeen.length} more` : ""}
        </p>
      )}
    </button>
  );
}

/** Construct coverage per platform; shown only when the snapshot met a platform pack's constructs. */
function PlatformStrip({ platforms, onOpen }: { platforms: PlatformCoverage[]; onOpen: () => void }) {
  if (platforms.length === 0) return null;
  return (
    <section className="platform-strip" aria-label="Platform constructs">
      <header>
        <h3>Platform constructs</h3>
        <p>How much of each platform&apos;s own vocabulary the compiler modelled. A construct seen but not modelled is still counted, so a gap here is known rather than hidden.</p>
      </header>
      <div className="platform-cards">
        {platforms.map((p) => (
          <PlatformCard key={p.platform} platform={p} onOpen={onOpen} />
        ))}
      </div>
    </section>
  );
}

export function MapView({
  summary,
  prefs,
  onPrefs,
  onOpenPlane,
  onOpenQuality,
  purpose,
  onOpenPurpose,
}: {
  purpose?: Purpose | null;
  onOpenPurpose?: () => void;
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
          <p>{PLANES.length} planes compiled from one commit, plus what the compiler could not resolve.</p>
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
        {purpose && purpose.capabilities.length > 0 && onOpenPurpose && <PurposeCard purpose={purpose} onOpen={onOpenPurpose} />}
        {PLANES.map((plane, i) => (
          <PlaneCard key={plane.id} plane={plane} summary={summary} index={i} onOpen={() => onOpenPlane(plane.id)} />
        ))}
        <QualityCard summary={summary} index={PLANES.length} onOpen={onOpenQuality} />
      </div>

      <PlatformStrip platforms={platformCoverage(summary)} onOpen={onOpenQuality} />

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
