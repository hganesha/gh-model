/**
 * The Explorer's views in a VS Code webview, over the repository's own `.rkp/` bundle.
 *
 * The views are the Explorer's components, imported unchanged from
 * repo-intelligence/explorer ("@/..."). This file replaces only its outer shell: one
 * repository and one snapshot (the bundle's), no sidebar, no connection or settings
 * screens, and no business map, timeline or lineage, which need more than one bundle.
 */

import "@fontsource-variable/fraunces";
import "@fontsource-variable/hanken-grotesk";
import "@fontsource-variable/jetbrains-mono";
import "@/app/globals.css";
import "./webview.css";

import { FileCode2, GitBranch, RefreshCw, Search } from "lucide-react";
import { StrictMode, useCallback, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { DecisionsView } from "@/components/decisions-view";
import { EntityDrawer } from "@/components/entity-drawer";
import { DEFAULT_GRAPH_PREFS, GraphView, type GraphPrefs } from "@/components/graph-view";
import { MapView, type MapPrefs } from "@/components/map-view";
import { PlaneView } from "@/components/plane-view";
import { QualityView } from "@/components/quality-view";
import { SearchDialog } from "@/components/search-dialog";
import { Badge, ErrorNote, Loading } from "@/components/ui";
import { snapshotPath, useApi } from "@/lib/api";
import { shortSha } from "@/lib/format";
import type { EntityRef, Summary } from "@/lib/types";
import { installFetchBridge, onReload, vscode } from "./bridge";

installFetchBridge();

// VS Code marks the theme on <body>; the Explorer's dark tokens key off html.rkp-dark.
function followTheme(): void {
  const dark = document.body.classList.contains("vscode-dark") || (document.body.classList.contains("vscode-high-contrast") && !document.body.classList.contains("vscode-high-contrast-light"));
  document.documentElement.classList.toggle("rkp-dark", dark);
  document.documentElement.style.colorScheme = dark ? "dark" : "light";
}
followTheme();
new MutationObserver(followTheme).observe(document.body, { attributes: true, attributeFilter: ["class"] });

type View = "map" | "graph" | "quality" | "decisions";
const VIEWS: { id: View; label: string }[] = [
  { id: "map", label: "Map" },
  { id: "graph", label: "Graph" },
  { id: "quality", label: "Quality" },
  { id: "decisions", label: "Decisions" },
];

interface Local {
  repository: string;
  repository_id: string;
  rkp_hash: string;
  commit_sha: string;
  working_tree: boolean;
  status: string;
}

function App() {
  const [revision, setRevision] = useState(0);
  useEffect(() => onReload(() => setRevision((n) => n + 1)), []);
  const local = useApi<Local>(`local?r=${revision}`);
  if (local.loading) return <main className="rkp-webview-main"><Loading label="Reading .rkp/knowledge/" /></main>;
  if (local.error || !local.data) {
    return (
      <main className="rkp-webview-main">
        {local.error?.code === "missing"
          ? <section className="welcome"><span className="eyebrow">NO KNOWLEDGE</span><h2>This repository has not been analysed yet.</h2><p>Run <b>Analyze Repository</b> from the Repository Intelligence view; this page fills in when it finishes.</p></section>
          : <ErrorNote error={local.error!} onRetry={local.reload} what="the repository's knowledge" />}
      </main>
    );
  }
  return <Snapshot key={local.data.rkp_hash} local={local.data} />;
}

function Snapshot({ local }: { local: Local }) {
  const hash = local.rkp_hash;
  const [view, setView] = useState<View>("map");
  const [plane, setPlane] = useState<string | null>(null);
  const [trail, setTrail] = useState<string[]>([]);
  const [graphSeed, setGraphSeed] = useState<EntityRef | null>(null);
  const [searchOpen, setSearchOpen] = useState(false);
  const [mapPrefs, setMapPrefs] = useState<MapPrefs>({ zoom: 1, grid: true });
  const [graphPrefs, setGraphPrefs] = useState<GraphPrefs>(DEFAULT_GRAPH_PREFS);
  const summary = useApi<Summary>(snapshotPath(hash, "summary"));
  const activeEntityId = trail[trail.length - 1] ?? null;

  const openEntity = useCallback((id: string) => setTrail([id]), []);
  const navigateEntity = useCallback((id: string) => setTrail((t) => (t[t.length - 1] === id ? t : [...t, id])), []);

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setSearchOpen((open) => !open);
      } else if (e.key === "Escape") {
        if (searchOpen) setSearchOpen(false);
        else if (trail.length) setTrail([]);
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [searchOpen, trail.length]);

  function switchView(next: View) {
    setView(next);
    setPlane(null);
  }

  const status = summary.data?.manifest?.status;
  const name = local.repository_id.split("/").slice(-2).join("/");
  return (
    <div className={`shell rkp-webview ${trail.length ? "has-drawer" : ""}`}>
      <div className="workspace">
        <header className="topbar">
          <div className="topbar-repo">
            <h1>{name}</h1>
            <p>
              <span className="topbar-commit">
                <GitBranch size={12} aria-hidden="true" />
                <code>{shortSha(local.commit_sha)}</code>
                {local.working_tree && <span>with uncommitted changes</span>}
              </span>
              <Badge tone={status === "complete" ? "verified" : status ? "candidate" : "neutral"}>{summary.loading ? "loading" : (status ?? "status unknown")}</Badge>
            </p>
          </div>
          <div className="view-switch" role="tablist" aria-label="View">
            {VIEWS.map((v) => (
              <button type="button" key={v.id} role="tab" aria-selected={view === v.id} className={view === v.id ? "is-on" : ""} onClick={() => switchView(v.id)}>
                {v.label}
              </button>
            ))}
          </div>
          {activeEntityId && (
            <button type="button" className="search-trigger" onClick={() => vscode.postMessage({ type: "openEntity", id: activeEntityId })} title="Open the selected entity's evidence in the editor">
              <FileCode2 size={14} aria-hidden="true" />
              <span>Open source</span>
            </button>
          )}
          <button type="button" className="search-trigger" onClick={() => setSearchOpen(true)} aria-label="Search entities" aria-keyshortcuts="Meta+K Control+K">
            <Search size={14} aria-hidden="true" />
            <span>Search</span>
            <kbd>⌘K</kbd>
          </button>
          <button type="button" className="icon-btn" onClick={() => vscode.postMessage({ type: "analyze" })} aria-label="Analyze the repository again" title="Analyze Repository">
            <RefreshCw size={15} aria-hidden="true" />
          </button>
        </header>

        <main id="main" tabIndex={-1}>
          {summary.loading && <Loading label="Loading snapshot" />}
          {summary.error && <ErrorNote error={summary.error} onRetry={summary.reload} what="this snapshot" />}
          {summary.data && (
            <>
              {view === "map" && (plane === null
                ? <MapView summary={summary.data} prefs={mapPrefs} onPrefs={setMapPrefs} onOpenPlane={setPlane} onOpenQuality={() => switchView("quality")} purpose={null} />
                : <PlaneView key={`${hash}:${plane}`} hash={hash} planeId={plane} summary={summary.data} onBack={() => setPlane(null)} onOpenEntity={openEntity} activeEntityId={activeEntityId} />)}
              {view === "graph" && (
                <GraphView hash={hash} seed={graphSeed} prefs={graphPrefs} onPrefs={setGraphPrefs} onResetSeed={() => setGraphSeed(null)} onOpenEntity={openEntity} selectedId={activeEntityId} />
              )}
              {view === "quality" && <QualityView hash={hash} summary={summary.data} onOpenEntity={openEntity} />}
              {view === "decisions" && <DecisionsView hash={hash} repositoryId={local.repository_id} onOpenEntity={openEntity} />}
            </>
          )}
        </main>
      </div>

      {trail.length > 0 && (
        <EntityDrawer
          hash={hash}
          repositoryId={local.repository_id}
          snapshotLabel={`${shortSha(local.commit_sha)}${local.working_tree ? " + uncommitted" : ""}`}
          otherSnapshot={false}
          trail={trail}
          onNavigate={navigateEntity}
          onBack={() => setTrail((t) => t.slice(0, -1))}
          onClose={() => setTrail([])}
          onPlacementChanged={() => undefined}
          onViewInGraph={(ref) => {
            setGraphSeed(ref);
            setView("graph");
            setPlane(null);
            setTrail([]);
          }}
        />
      )}
      {searchOpen && <SearchDialog hash={hash} onClose={() => setSearchOpen(false)} onOpenEntity={openEntity} />}
    </div>
  );
}

createRoot(document.getElementById("root")!).render(<StrictMode><App /></StrictMode>);
