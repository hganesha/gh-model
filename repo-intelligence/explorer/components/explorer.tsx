"use client";

import { GitBranch, GitCommitHorizontal, GitCompareArrows, Lock, Menu, Plus, RefreshCw, Search, Settings, Workflow, Unlock, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { snapshotPath, useApi } from "@/lib/api";
import { relativeTime, shortSha } from "@/lib/format";
import type { EntityRef, Job, RepositoriesResponse, Repository, SnapshotRef, Summary, ViewId } from "@/lib/types";
import { Account } from "./account";
import { ConnectDialog, type ConnectTarget } from "./connect-dialog";
import { DecisionsView } from "./decisions-view";
import { EntityDrawer } from "./entity-drawer";
import { DEFAULT_GRAPH_PREFS, GraphView, type GraphPrefs } from "./graph-view";
import { MapView, ZOOM_STEPS, type MapPrefs, type Purpose } from "./map-view";
import { PlaneView } from "./plane-view";
import { QualityView } from "./quality-view";
import { SearchDialog } from "./search-dialog";
import { TimelineView, type ComparePick } from "./timeline-view";
import { BusinessMapView } from "./business-map-view";
import { SettingsView } from "./settings-view";
import { isPlaced, type SnapshotPlacements } from "@/lib/workspace";
import { Badge, ErrorNote, Loading } from "./ui";
import { RetractedNote, SnapshotActions, SnapshotStateBadge, snapshotState } from "./snapshot-actions";
import { RefOpener } from "./ref-opener";
import { PREDICATES } from "@/lib/planes";
import { commitDates, orderPair } from "@/lib/snapshots";

const VIEWS: { id: ViewId; label: string }[] = [
  { id: "map", label: "Map" },
  { id: "graph", label: "Graph" },
  { id: "timeline", label: "Timeline" },
  { id: "quality", label: "Quality" },
  { id: "decisions", label: "Decisions" },
];

// View preferences only (never API data). Storage can be unavailable, so every access is guarded.
const PREFS_KEY = "rkp-explorer:view-prefs";
interface Prefs {
  map: MapPrefs;
  graph: GraphPrefs;
}
const DEFAULT_PREFS: Prefs = { map: { zoom: 1, grid: true }, graph: DEFAULT_GRAPH_PREFS };

function loadPrefs(): Prefs {
  try {
    const raw = window.localStorage.getItem(PREFS_KEY);
    if (!raw) return DEFAULT_PREFS;
    const p = JSON.parse(raw) as Partial<Prefs>;
    const zoom = ZOOM_STEPS.includes(p.map?.zoom as number) ? (p.map!.zoom as number) : 1;
    const depth = [1, 2, 3].includes(p.graph?.depth as number) ? (p.graph!.depth as number) : DEFAULT_GRAPH_PREFS.depth;
    const predicates = Array.isArray(p.graph?.predicates) ? PREDICATES.filter((x) => p.graph!.predicates!.includes(x)) : DEFAULT_GRAPH_PREFS.predicates;
    return { map: { zoom, grid: p.map?.grid !== false }, graph: { depth, predicates } };
  } catch {
    return DEFAULT_PREFS;
  }
}

function savePrefs(prefs: Prefs) {
  try {
    window.localStorage.setItem(PREFS_KEY, JSON.stringify(prefs));
  } catch {
    /* preferences are optional */
  }
}

/** Workspace views are addressable (?workspace=business) so a reload or a shared link lands on them. */
function readWorkspaceUrl(): "business" | "settings" | null {
  const value = new URLSearchParams(window.location.search).get("workspace");
  return value === "business" || value === "settings" ? value : null;
}

function writeWorkspaceUrl(next: "business" | "settings" | null) {
  try {
    const url = new URL(window.location.href);
    if (next) url.searchParams.set("workspace", next);
    else url.searchParams.delete("workspace");
    window.history.replaceState(null, "", url);
  } catch {
    /* the address bar is a convenience */
  }
}

function sortedSnapshots(repo: Repository): SnapshotRef[] {
  return [...(repo.snapshots ?? [])].sort((a, b) => b.compiled_at - a.compiled_at);
}

/** Topology lives on the snapshot summary, so other repositories fetch theirs on demand. */
function RepoTopology({ repo, known }: { repo: Repository; known: string | null | undefined }) {
  const latest = sortedSnapshots(repo)[0];
  const { data, error } = useApi<Summary>(known === undefined && latest ? snapshotPath(latest.rkp_hash, "summary") : null);
  const topology = known !== undefined ? known : data?.topology;
  if (!latest) return <span>not compiled</span>;
  if (error) return <span>topology unknown</span>;
  return <span>{topology === undefined ? "…" : (topology ?? "topology unknown")}</span>;
}

export function Explorer() {
  const repos = useApi<RepositoriesResponse>("repositories");
  const [repoId, setRepoId] = useState<string | null>(null);
  const [hashChoice, setHashChoice] = useState<string | null>(null);
  const [view, setView] = useState<ViewId>("map");
  const [plane, setPlane] = useState<string | null>(null);
  const [trail, setTrail] = useState<string[]>([]);
  // The drawer can show an entity from a snapshot other than the selected one (e.g. something removed in a diff).
  const [drawerHash, setDrawerHash] = useState<string | null>(null);
  const [pick, setPick] = useState<ComparePick | null>(null);
  const [graphSeed, setGraphSeed] = useState<EntityRef | null>(null);
  const [searchOpen, setSearchOpen] = useState(false);
  const [connect, setConnect] = useState<ConnectTarget | null>(null);
  const [navOpen, setNavOpen] = useState(false);
  const [prefs, setPrefs] = useState<Prefs>(DEFAULT_PREFS);
  const [workspace, setWorkspace] = useState<"business" | "settings" | null>(null);
  const [placementRevision, setPlacementRevision] = useState(0);
  const [now, setNow] = useState<number | null>(null);

  useEffect(() => {
    setPrefs(loadPrefs());
    setWorkspace(readWorkspaceUrl());
    setNow(Date.now());
    const t = setInterval(() => setNow(Date.now()), 60_000);
    return () => clearInterval(t);
  }, []);

  const updatePrefs = useCallback((next: Prefs) => {
    setPrefs(next);
    savePrefs(next);
  }, []);

  const leaveWorkspace = useCallback(() => {
    setWorkspace(null);
    writeWorkspaceUrl(null);
  }, []);

  const repositories = useMemo(() => repos.data?.repositories ?? [], [repos.data]);
  const repo = repositories.find((r) => r.repository_id === repoId) ?? repositories[0] ?? null;
  const snapshots = useMemo(() => (repo ? sortedSnapshots(repo) : []), [repo]);
  // By default open the newest readable snapshot; a retracted or quarantined one only when chosen.
  const snapshot = snapshots.find((s) => s.rkp_hash === hashChoice) ?? snapshots.find((s) => !snapshotState(s)) ?? snapshots[0] ?? null;
  const hash = snapshot?.rkp_hash ?? null;

  const placements = useApi<SnapshotPlacements>(hash && !workspace ? snapshotPath(hash, "placements") : null);
  const summary = useApi<Summary>(hash ? snapshotPath(hash, "summary") : null);

  const resetSnapshotState = useCallback(() => {
    setTrail([]);
    setDrawerHash(null);
    setGraphSeed(null);
    setPlane(null);
  }, []);

  function selectRepo(id: string) {
    leaveWorkspace();
    setRepoId(id);
    setHashChoice(null);
    setPick(null);
    resetSnapshotState();
    setNavOpen(false);
  }

  function selectSnapshot(next: string) {
    leaveWorkspace();
    setHashChoice(next);
    setPick(null);
    resetSnapshotState();
    setNavOpen(false);
  }

  /** Open Timeline comparing another snapshot with the selected one; the older commit becomes "from". */
  function compareWith(other: SnapshotRef) {
    leaveWorkspace();
    if (!snapshot) return;
    const { from, to } = orderPair(other, snapshot, commitDates(snapshots));
    setPick({ from: from.rkp_hash, to: to.rkp_hash });
    setView("timeline");
    setPlane(null);
    setNavOpen(false);
  }

  function switchView(next: ViewId) {
    leaveWorkspace();
    setView(next);
    setPlane(null);
  }

  const openEntity = useCallback((id: string, inHash?: string) => {
    setDrawerHash(inHash ?? null);
    setTrail([id]);
  }, []);
  const navigateEntity = useCallback((id: string) => setTrail((t) => (t[t.length - 1] === id ? t : [...t, id])), []);

  function onSnapshot(job: Job) {
    leaveWorkspace();
    repos.reload();
    setRepoId(job.repository_id);
    setHashChoice(job.rkp_hash);
    setPick(null);
    resetSnapshotState();
    setView("map");
  }

  // Global keys: Cmd/Ctrl+K opens search; Escape closes the top-most layer only.
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setSearchOpen((open) => !open);
        return;
      }
      if (e.key !== "Escape") return;
      if (searchOpen) setSearchOpen(false);
      else if (connect) setConnect(null);
      else if (trail.length) setTrail([]);
      else if (navOpen) setNavOpen(false);
      else return;
      e.preventDefault();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [searchOpen, connect, trail.length, navOpen]);

  const drawerOpen = trail.length > 0 && hash !== null;
  const shownHash = drawerHash ?? hash;
  const shownSnapshot = snapshots.find((s) => s.rkp_hash === shownHash) ?? null;
  const activeEntityId = shownHash === hash ? (trail[trail.length - 1] ?? null) : null;
  const status = summary.data?.manifest?.status;
  const purpose = useMemo<Purpose | null>(() => {
    const rows = Object.values(placements.data?.components ?? {}).filter((row) => isPlaced(row) && row.capability);
    if (!rows.length) return null;
    const byName = new Map<string, { name: string; components: number; ai: number }>();
    for (const row of rows) {
      const entry = byName.get(row.capability!) ?? { name: row.capability!, components: 0, ai: 0 };
      entry.components += 1;
      if (row.status === "ai") entry.ai += 1;
      byName.set(entry.name, entry);
    }
    return {
      capabilities: [...byName.values()].sort((a, b) => b.components - a.components || a.name.localeCompare(b.name)),
      placed: rows.length,
      components: Math.max(rows.length, placements.data?.eligible_components ?? 0),
    };
  }, [placements.data]);
  function openWorkspace(next: "business" | "settings") {
    setWorkspace(next);
    writeWorkspaceUrl(next);
    setNavOpen(false);
    setTrail([]);
  }

  return (
    <div className={`shell ${drawerOpen ? "has-drawer" : ""}`}>
      <a className="skip-link" href="#main">
        Skip to content
      </a>

      {navOpen && <div className="nav-scrim" onClick={() => setNavOpen(false)} aria-hidden="true" />}
      <nav className={`sidebar ${navOpen ? "is-open" : ""}`} aria-label="Explorer navigation">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true">
            <i />
            <i />
            <i />
          </span>
          <span className="brand-name">
            RKP Explorer<span className="brand-period">.</span>
          </span>
          <button type="button" className="icon-btn nav-close" aria-label="Close navigation" onClick={() => setNavOpen(false)}>
            <X size={16} aria-hidden="true" />
          </button>
        </div>

        <div className="side-section"><h2>Workspace</h2>
          <button type="button" className={`btn btn-side ${workspace === "business" ? "is-selected" : ""}`} aria-current={workspace === "business" ? "page" : undefined}
            onClick={() => openWorkspace("business")}><Workflow size={14} aria-hidden="true" /> Business map</button>
        </div>

        <div className="side-section">
          <h2>Repositories</h2>
          {repos.loading && <Loading label="Loading repositories" />}
          {repos.error && <ErrorNote error={repos.error} onRetry={repos.reload} what="repositories" />}
          {repos.data && repositories.length === 0 && <p className="side-note">None connected yet.</p>}
          <ul className="repo-list">
            {repositories.map((r) => {
              const selected = !workspace && r.repository_id === repo?.repository_id;
              return (
                <li key={r.repository_id}>
                  <button type="button" className={`repo-item ${selected ? "is-selected" : ""}`} aria-current={selected ? "true" : undefined} onClick={() => selectRepo(r.repository_id)}>
                    <span className="repo-name">{r.full_name}</span>
                    <span className="repo-meta">
                      {r.private ? <Lock size={11} aria-hidden="true" /> : <Unlock size={11} aria-hidden="true" />}
                      {r.private ? "private" : "public"}
                      <i aria-hidden="true">·</i>
                      <RepoTopology repo={r} known={selected && hash === sortedSnapshots(r)[0]?.rkp_hash && summary.data ? summary.data.topology : undefined} />
                    </span>
                  </button>
                </li>
              );
            })}
          </ul>
          <button type="button" className="btn btn-side" onClick={() => setConnect({ mode: "connect" })}>
            <Plus size={14} aria-hidden="true" /> Connect repository
          </button>
        </div>

        {repo && !workspace && (
          <div className="side-section">
            <div className="side-head">
              <h2>Snapshots</h2>
              <button type="button" className="side-action" onClick={() => setConnect({ mode: "recompile", repository: repo, ref: snapshot?.ref ?? repo.default_branch ?? null })}>
                <RefreshCw size={12} aria-hidden="true" /> Recompile
              </button>
            </div>
            {snapshots.length === 0 && <p className="side-note">No snapshot has been published for this repository yet. Recompile to try again.</p>}
            <ul className="snapshot-list">
              {snapshots.map((s) => {
                const selected = s.rkp_hash === hash;
                return (
                  <li key={s.rkp_hash}>
                    <button type="button" className={`snapshot-item ${selected ? "is-selected" : ""} ${s.retracted ? "is-retracted" : ""}`} aria-current={selected ? "true" : undefined} onClick={() => selectSnapshot(s.rkp_hash)}>
                      <GitCommitHorizontal size={13} aria-hidden="true" />
                      <code>{shortSha(s.commit_sha)}</code>
                      <span className="snapshot-ref">{s.ref}<SnapshotStateBadge snapshot={s} /></span>
                      <time dateTime={new Date(s.compiled_at * 1000).toISOString()}>{now === null ? "" : relativeTime(s.compiled_at, now)}</time>
                    </button>
                    {!selected && !s.retracted && (
                      <button type="button" className="snapshot-compare" onClick={() => compareWith(s)} aria-label={`Compare ${shortSha(s.commit_sha)} ${s.ref} with the selected snapshot`} title="Compare with the selected snapshot">
                        <GitCompareArrows size={13} aria-hidden="true" />
                        <span>Compare</span>
                      </button>
                    )}
                  </li>
                );
              })}
            </ul>
            <RefOpener repo={repo} onOpen={(next) => { repos.reload(); selectSnapshot(next); }} onPending={repos.reload} />
          </div>
        )}

        <div className="side-section workspace-nav-bottom">
          <button type="button" className="btn btn-side" aria-current={workspace === "settings" ? "page" : undefined}
            onClick={() => openWorkspace("settings")}><Settings size={14} aria-hidden="true" /> Settings</button>
        </div>
      </nav>

      <div className="workspace">
        <header className="topbar">
          <button type="button" className="icon-btn nav-open" aria-label="Open navigation" onClick={() => setNavOpen(true)}>
            <Menu size={18} aria-hidden="true" />
          </button>
          <div className="topbar-repo">
            <h1>{workspace ? "Workspace" : (repo ? repo.full_name : "RKP Explorer")}</h1>
            {!workspace && snapshot && (
              <p>
                <span className="topbar-commit">
                  <GitBranch size={12} aria-hidden="true" />
                  {snapshot.ref}
                  <code>{shortSha(snapshot.commit_sha)}</code>
                </span>
                <Badge tone={status === "complete" ? "verified" : status === "retracted" || snapshot.retracted ? "danger" : status ? "candidate" : "neutral"}>{summary.loading ? "loading" : (status ?? (snapshot.retracted ? "retracted" : "status unknown"))}</Badge>
              </p>
            )}
            {!workspace && snapshot && repo && <div className="topbar-actions"><SnapshotActions repo={repo} snapshot={snapshot} snapshots={snapshots} onChanged={(next) => { repos.reload(); if (next) selectSnapshot(next); summary.reload(); }} /></div>}
          </div>
          {!workspace && <div className="view-switch" role="tablist" aria-label="View">
            {VIEWS.map((v) => (
              <button type="button" key={v.id} role="tab" aria-selected={view === v.id} className={view === v.id ? "is-on" : ""} onClick={() => switchView(v.id)} disabled={!hash}>
                {v.label}
              </button>
            ))}
          </div>}
          <button type="button" className="search-trigger" onClick={() => setSearchOpen(true)} aria-label="Search entities" aria-keyshortcuts="Meta+K Control+K">
            <Search size={14} aria-hidden="true" />
            <span>Search</span>
            <kbd>⌘K</kbd>
          </button>
          <Account />
        </header>

        <main id="main" tabIndex={-1}>
          {workspace === "business" && <BusinessMapView key={placementRevision} onOpen={openEntity} onOpenSettings={() => openWorkspace("settings")} />}
          {workspace === "settings" && <SettingsView />}
          {!workspace && <>
          {repos.loading && <Loading label="Loading repositories" />}
          {repos.error && <ErrorNote error={repos.error} onRetry={repos.reload} what="repositories" />}

          {repos.data && repositories.length === 0 && (
            <section className="welcome">
              <span className="eyebrow">NOTHING CONNECTED</span>
              <h2>Connect a repository to see what the compiler can know about it.</h2>
              <p>RKP reads one commit and compiles it into structure, interfaces, data, AI, tests and deployment facts — each with its evidence, and each gap marked as a gap.</p>
              <button type="button" className="btn btn-primary" onClick={() => setConnect({ mode: "connect" })}>
                <Plus size={14} aria-hidden="true" /> Connect repository
              </button>
            </section>
          )}

          {repo && !snapshot && (
            <section className="welcome">
              <span className="eyebrow">NO SNAPSHOT</span>
              <h2>This repository is connected but has no published snapshot.</h2>
              <p>A compilation may still be running, or the last one failed. Recompile to produce a snapshot.</p>
              <button type="button" className="btn btn-primary" onClick={() => setConnect({ mode: "recompile", repository: repo, ref: repo.default_branch ?? null })}>
                <RefreshCw size={14} aria-hidden="true" /> Recompile
              </button>
            </section>
          )}

          {hash && summary.loading && <Loading label="Loading snapshot" />}
          {hash && summary.error && (summary.error.code === "retracted"
            ? <RetractedNote error={summary.error} snapshots={snapshots} onOpen={selectSnapshot} />
            : <ErrorNote error={summary.error} onRetry={summary.reload} what="this snapshot" />)}

          {hash && summary.data && (
            <>
              {view === "map" && plane === null && repo?.description && <p className="repo-description">{repo.description}</p>}
              {view === "map" &&
                (plane === null ? (
                  <MapView summary={summary.data} prefs={prefs.map} onPrefs={(map) => updatePrefs({ ...prefs, map })} onOpenPlane={setPlane} onOpenQuality={() => switchView("quality")} purpose={purpose} onOpenPurpose={() => openWorkspace("business")} />
                ) : (
                  <PlaneView key={`${hash}:${plane}`} hash={hash} planeId={plane} summary={summary.data} onBack={() => setPlane(null)} onOpenEntity={openEntity} activeEntityId={activeEntityId} />
                ))}
              {view === "graph" && (
                <GraphView
                  hash={hash}
                  seed={graphSeed}
                  prefs={prefs.graph}
                  onPrefs={(graph) => updatePrefs({ ...prefs, graph })}
                  onResetSeed={() => setGraphSeed(null)}
                  onOpenEntity={openEntity}
                  selectedId={activeEntityId}
                />
              )}
              {view === "timeline" && repo && (
                <TimelineView
                  repo={repo}
                  snapshots={snapshots}
                  selectedHash={hash}
                  pick={pick}
                  onPick={setPick}
                  onOpenEntity={openEntity}
                  onRecompile={() => setConnect({ mode: "recompile", repository: repo, ref: null })}
                  now={now}
                />
              )}
              {view === "quality" && <QualityView hash={hash} summary={summary.data} onOpenEntity={openEntity} />}
              {view === "decisions" && <DecisionsView hash={hash} repositoryId={repo?.repository_id ?? null} onOpenEntity={openEntity} />}
            </>
          )}
          </>}
        </main>
      </div>

      {drawerOpen && shownHash && (
        <EntityDrawer
          hash={shownHash}
          repositoryId={repo?.repository_id ?? null}
          onPlacementChanged={() => { setPlacementRevision((n) => n + 1); placements.reload(); }}
          snapshotLabel={shownSnapshot ? `${shortSha(shownSnapshot.commit_sha)} · ${shownSnapshot.ref}` : "unknown snapshot"}
          otherSnapshot={shownHash !== hash}
          trail={trail}
          onNavigate={navigateEntity}
          onBack={() => setTrail((t) => t.slice(0, -1))}
          onClose={() => setTrail([])}
          onViewInGraph={(ref) => {
            // The graph always draws the selected snapshot, so select the one this entity came from.
            if (shownHash !== hash) setHashChoice(shownHash);
            setDrawerHash(null);
            setGraphSeed(ref);
            setView("graph");
            setPlane(null);
            setTrail([]);
          }}
        />
      )}

      {searchOpen && <SearchDialog hash={hash} onClose={() => setSearchOpen(false)} onOpenEntity={openEntity} />}
      {connect && <ConnectDialog target={connect} onClose={() => setConnect(null)} onSnapshot={onSnapshot} />}
    </div>
  );
}
