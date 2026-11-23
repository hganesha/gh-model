"use client";

import { ChevronDown, ChevronRight, GitCommitHorizontal, RefreshCw } from "lucide-react";
import { useMemo, useState } from "react";
import { relativeTime, shortSha } from "@/lib/format";
import { commitDates, orderPair, type OrderedPair } from "@/lib/snapshots";
import type { Repository, SnapshotRef } from "@/lib/types";
import { DiffView } from "./diff-view";

export interface ComparePick {
  from: string;
  to: string;
}

interface CommitGroup {
  commit: string;
  /** Newest compile first. */
  compiles: SnapshotRef[];
}

/**
 * Default pair: the selected snapshot against the nearest different commit with
 * an older commit date; when the selected one is the oldest, the next newer one
 * (the pair is then swapped so "from" is still the older commit). Compile time
 * stands in only when a commit date is unknown.
 */
function defaultPair(snapshots: SnapshotRef[], selectedHash: string, dates: Map<string, number>): OrderedPair | null {
  const selected = snapshots.find((s) => s.rkp_hash === selectedHash);
  if (!selected) return null;
  // One candidate per other commit: its latest compile (snapshots arrive newest compile first).
  const seen = new Set<string>([selected.commit_sha]);
  const others = snapshots.filter((s) => !seen.has(s.commit_sha) && seen.add(s.commit_sha));
  if (!others.length) return null;
  const allDated = dates.has(selected.commit_sha) && others.every((s) => dates.has(s.commit_sha));
  const at = (s: SnapshotRef) => (allDated ? dates.get(s.commit_sha)! : s.compiled_at);
  const older = others.filter((s) => at(s) < at(selected)).sort((a, b) => at(b) - at(a));
  const newer = others.filter((s) => at(s) >= at(selected)).sort((a, b) => at(a) - at(b));
  return orderPair(older[0] ?? newer[0], selected, dates);
}

function SnapshotRow({
  snapshot,
  commitDate,
  role,
  now,
  nested,
  onFrom,
  onTo,
  children,
}: {
  snapshot: SnapshotRef;
  /** Commit date in epoch seconds, when known. */
  commitDate: number | undefined;
  role: "from" | "to" | null;
  now: number | null;
  nested?: boolean;
  onFrom: () => void;
  onTo: () => void;
  children?: React.ReactNode;
}) {
  return (
    <div className={`commit-row ${nested ? "is-nested" : ""} ${role ? `is-${role}` : ""}`}>
      <span className="commit-node" aria-hidden="true">
        {!nested && <GitCommitHorizontal size={13} />}
      </span>
      <div className="commit-main">
        <span className="commit-ref">{nested ? "compile" : snapshot.ref}</span>
        <code>{shortSha(snapshot.commit_sha)}</code>
        {!nested &&
          (commitDate !== undefined ? (
            <time className="commit-date" dateTime={new Date(commitDate * 1000).toISOString()} title={`Committed ${new Date(commitDate * 1000).toISOString().replace("T", " ").replace(/\.\d+Z$/, " UTC")}`}>
              committed {now === null ? "" : relativeTime(commitDate, now)}
            </time>
          ) : (
            <span className="commit-date is-unknown">commit date unknown</span>
          ))}
        <time className="compile-time" dateTime={new Date(snapshot.compiled_at * 1000).toISOString()} title={`Compiled ${new Date(snapshot.compiled_at * 1000).toISOString().replace("T", " ").replace(/\.\d+Z$/, " UTC")}`}>
          compiled {now === null ? "" : relativeTime(snapshot.compiled_at, now)}
        </time>
        {children}
      </div>
      <div className="commit-pick" role="group" aria-label={`Use ${shortSha(snapshot.commit_sha)} ${snapshot.ref} in the comparison`}>
        <button type="button" className={`pick ${role === "from" ? "is-on" : ""}`} aria-pressed={role === "from"} onClick={onFrom}>
          From
        </button>
        <button type="button" className={`pick ${role === "to" ? "is-on" : ""}`} aria-pressed={role === "to"} onClick={onTo}>
          To
        </button>
      </div>
    </div>
  );
}

export function TimelineView({
  repo,
  snapshots,
  selectedHash,
  pick,
  onPick,
  onOpenEntity,
  onRecompile,
  now,
}: {
  repo: Repository;
  /** Newest compile first. */
  snapshots: SnapshotRef[];
  selectedHash: string;
  pick: ComparePick | null;
  onPick: (next: ComparePick) => void;
  onOpenEntity: (id: string, hash: string) => void;
  onRecompile: () => void;
  now: number | null;
}) {
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});

  const dates = useMemo(() => commitDates(snapshots), [snapshots]);

  const groups = useMemo(() => {
    const map = new Map<string, CommitGroup>();
    for (const s of snapshots) {
      if (!map.has(s.commit_sha)) map.set(s.commit_sha, { commit: s.commit_sha, compiles: [] });
      map.get(s.commit_sha)!.compiles.push(s);
    }
    const list = [...map.values()];
    // Newest commit first when every commit has a date; otherwise newest compile first.
    if (list.every((g) => dates.has(g.commit))) list.sort((a, b) => dates.get(b.commit)! - dates.get(a.commit)!);
    return list;
  }, [snapshots, dates]);
  const byCommitDate = groups.length > 0 && groups.every((g) => dates.has(g.commit));

  const pair = useMemo(() => {
    const a = pick ? snapshots.find((s) => s.rkp_hash === pick.from) : undefined;
    const b = pick ? snapshots.find((s) => s.rkp_hash === pick.to) : undefined;
    if (a && b && a !== b) return orderPair(a, b, dates);
    return defaultPair(snapshots, selectedHash, dates);
  }, [pick, snapshots, selectedHash, dates]);

  function choose(side: "from" | "to", snapshot: SnapshotRef) {
    const fallback = snapshots.find((s) => s.rkp_hash === selectedHash) ?? snapshots[0];
    const current = pair ?? { from: fallback, to: fallback };
    // Picking the snapshot that already holds the other side swaps the two.
    let next = side === "from" ? { from: snapshot, to: current.to } : { from: current.from, to: snapshot };
    if (next.from === next.to) next = side === "from" ? { from: snapshot, to: current.from } : { from: current.to, to: snapshot };
    if (next.from === next.to) return;
    const o = orderPair(next.from, next.to, dates);
    onPick({ from: o.from.rkp_hash, to: o.to.rkp_hash });
  }

  const roleOf = (s: SnapshotRef): "from" | "to" | null => (pair?.from === s ? "from" : pair?.to === s ? "to" : null);
  const sameCommit = pair ? pair.from.commit_sha === pair.to.commit_sha : false;

  return (
    <section className="timeline-view" aria-label="Timeline">
      <aside className="timeline-rail" aria-label="Snapshots by commit">
        <header>
          <span className="eyebrow">TIMELINE</span>
          <h2>Compiled commits</h2>
          <p>
            {byCommitDate
              ? "Newest commit first, by commit date. Pick the two to compare; the older commit is always “from”."
              : "Newest compile first, because not every commit date is known. Pick the two to compare; the older one is always “from”."}
          </p>
        </header>
        <ol className="commit-list">
          {groups.map((g) => {
            const latest = g.compiles[0];
            const rest = g.compiles.slice(1);
            const open = expanded[g.commit] ?? rest.some((s) => roleOf(s) !== null);
            return (
              <li key={g.commit} className="commit-group">
                <SnapshotRow snapshot={latest} commitDate={dates.get(latest.commit_sha)} role={roleOf(latest)} now={now} onFrom={() => choose("from", latest)} onTo={() => choose("to", latest)}>
                  {latest.rkp_hash === selectedHash && <span className="commit-selected">selected</span>}
                  {rest.length > 0 && (
                    <button type="button" className="compiles-toggle" aria-expanded={open} onClick={() => setExpanded((e) => ({ ...e, [g.commit]: !open }))}>
                      {open ? <ChevronDown size={12} aria-hidden="true" /> : <ChevronRight size={12} aria-hidden="true" />}
                      {g.compiles.length} compiles
                    </button>
                  )}
                </SnapshotRow>
                {open &&
                  rest.map((s) => (
                    <SnapshotRow key={s.rkp_hash} snapshot={s} commitDate={dates.get(s.commit_sha)} role={roleOf(s)} now={now} nested onFrom={() => choose("from", s)} onTo={() => choose("to", s)}>
                      {s.rkp_hash === selectedHash && <span className="commit-selected">selected</span>}
                    </SnapshotRow>
                  ))}
              </li>
            );
          })}
        </ol>
        <button type="button" className="btn btn-quiet btn-small" onClick={onRecompile}>
          <RefreshCw size={12} aria-hidden="true" /> Compile another commit
        </button>
      </aside>

      <div className="timeline-main">
        {!pair ? (
          <div className="empty-plane">
            <h3>A second commit must be compiled before there is anything to compare.</h3>
            <p>
              Every snapshot of <strong>{repo.full_name}</strong> so far was compiled from the same commit. Compile another branch, tag or commit to see what changed between the two.
              {snapshots.length > 1 && " To compare two compiles of this one commit, expand its compiles on the left and pick From and To."}
            </p>
            <button type="button" className="btn btn-primary" onClick={onRecompile}>
              <RefreshCw size={14} aria-hidden="true" /> Compile another commit
            </button>
          </div>
        ) : (
          <DiffView key={`${pair.from.rkp_hash}>${pair.to.rkp_hash}`} from={pair.from} to={pair.to} sameCommit={sameCommit} basis={pair.basis} onOpenEntity={onOpenEntity} />
        )}
      </div>
    </section>
  );
}
