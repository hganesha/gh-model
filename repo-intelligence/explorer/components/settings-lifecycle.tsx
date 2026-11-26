"use client";

import { Trash2 } from "lucide-react";
import { useState, type Dispatch, type SetStateAction } from "react";
import { api, ApiError, useApi } from "@/lib/api";
import type { ParityPolicy, RetentionPolicy, SettingsDocument, SettingsView as SettingsResponse, StepMetrics } from "@/lib/workspace";
import { ErrorNote, Loading, Modal } from "./ui";

type Update = Dispatch<SetStateAction<SettingsDocument>>;
type Status = { ok: boolean; text: string };

const RETENTION_DEFAULT: RetentionPolicy = { branch_days: 30, pull_request_days_after_close: 7, tag_days: null, blob_cache_days: 14, step_cache_days: 30, mirror_days: 14, webhook_delivery_days: 7 };
const PARITY_DEFAULT: ParityPolicy = { shadow_rate: 0.02, nightly_head: true };

const RETENTION_FIELDS: { key: keyof RetentionPolicy; label: string; min: number; hint: string }[] = [
  { key: "branch_days", label: "Branch snapshots (days)", min: 1, hint: "Each ref's current snapshot is always kept." },
  { key: "pull_request_days_after_close", label: "Pull requests after close (days)", min: 0, hint: "" },
  { key: "blob_cache_days", label: "Source blob cache (days)", min: 0, hint: "Evicted blobs are fetched again by Git object ID." },
  { key: "step_cache_days", label: "Step cache (days unused)", min: 0, hint: "" },
  { key: "mirror_days", label: "Git mirrors (days unused)", min: 1, hint: "" },
  { key: "webhook_delivery_days", label: "Webhook delivery IDs (days)", min: 3, hint: "GitHub redelivers for 3 days." },
];

/** What the compile requires before it publishes, and experimental extras. */
export function CompileProfile({ view, draft, update }: { view: SettingsResponse; draft: SettingsDocument; update: Update }) {
  const domains = view.coverage_domains ?? [];
  const required = draft.compile.required_coverage ?? [];
  function patch(change: Partial<SettingsDocument["compile"]>) {
    update((prev) => ({ ...prev, compile: { ...prev.compile, ...change } }));
  }
  return <section className="work-tool-panel"><h3>Compile profile</h3>
    {domains.length > 0 && <>
      <p className="group-note">The publish gate refuses a snapshot unless each required coverage domain was measured (it has a denominator).</p>
      <div className="work-tool-grid">{domains.map((d) => <label key={d} className="work-check">
        <input type="checkbox" checked={required.includes(d)} onChange={(e) => patch({ required_coverage: e.target.checked ? [...new Set([...required, d])] : required.filter((x) => x !== d) })} /> {d.replaceAll("_", " ")}
      </label>)}</div>
    </>}
    <label className="work-check"><input type="checkbox" checked={Boolean(draft.compile.behavior_candidates)} onChange={(e) => patch({ behavior_candidates: e.target.checked })} />
      Behaviour candidates (experimental): an LLM proposes features a component implements. They are stored as untrusted hypotheses and need an allowed LLM provider.</label>
    <label className="work-check"><input type="checkbox" checked={Boolean(draft.compile.business_rules)} onChange={(e) => patch({ business_rules: e.target.checked })} />
      <span>Business rules: a model names the rules that behaviour units enforce and that documents designated for rules state, and the workflows that state assignments follow. Rules stay hypotheses until an owner confirms them.
        <small>Sends repository content (source excerpts and designated document text) to the configured model provider.</small></span></label>
    <label className="work-check"><input type="checkbox" checked={Boolean(draft.compile.captured_screens)} onChange={(e) => patch({ captured_screens: e.target.checked })} />
      <span>Captured screens: a model reads the regions and controls of screens captured under <code>.rkp/inputs/screens/</code> and matches them to the screens in the code.
        <small>Sends repository content (the captured screens) to the configured model provider.</small></span></label>
  </section>;
}

export function RetentionAndParity({ draft, update }: { draft: SettingsDocument; update: Update }) {
  const retention = { ...RETENTION_DEFAULT, ...draft.retention };
  const parity = { ...PARITY_DEFAULT, ...draft.parity };
  const setRetention = (change: Partial<RetentionPolicy>) => update((prev) => ({ ...prev, retention: { ...RETENTION_DEFAULT, ...prev.retention, ...change } }));
  const setParity = (change: Partial<ParityPolicy>) => update((prev) => ({ ...prev, parity: { ...PARITY_DEFAULT, ...prev.parity, ...change } }));
  return <section className="work-tool-panel"><h3>Retention and parity</h3>
    <p className="group-note">Garbage collection keeps snapshots and caches for these periods and deletes the rest. A retracted snapshot is never kept.</p>
    <div className="work-tool-controls">
      {RETENTION_FIELDS.map((f) => <label className="field" key={f.key} title={f.hint || undefined}><span>{f.label}</span>
        <input type="number" min={f.min} max={3650} value={retention[f.key] ?? ""} onChange={(e) => setRetention({ [f.key]: Number(e.target.value) })} />
      </label>)}
      <label className="field"><span>Tag snapshots (days)</span>
        <input type="number" min={1} max={36500} placeholder="While the tag is tracked" value={retention.tag_days ?? ""} onChange={(e) => setRetention({ tag_days: e.target.value === "" ? null : Number(e.target.value) })} />
      </label>
    </div>
    <p className="group-note">A shadow compile repeats a sampled incremental compile from scratch. If the two disagree, the snapshot is quarantined and the cold result is published instead.</p>
    <div className="work-tool-controls">
      <label className="field"><span>Shadow-compile share (0–1)</span>
        <input type="number" min={0} max={1} step="0.01" value={parity.shadow_rate} onChange={(e) => setParity({ shadow_rate: Number(e.target.value) })} />
      </label>
      <label className="work-check"><input type="checkbox" checked={parity.nightly_head} onChange={(e) => setParity({ nightly_head: e.target.checked })} /> Also shadow-compile each default-branch head nightly</label>
    </div>
  </section>;
}

function StepMetricsTable() {
  const { data, error, loading, reload } = useApi<StepMetrics>("step-metrics");
  if (loading) return <Loading label="Loading step metrics" />;
  if (error) return error.status === 404 ? <p className="group-note">No compile has recorded step metrics yet.</p> : <ErrorNote error={error} onRetry={reload} what="step metrics" />;
  if (!data) return null;
  const rows = Object.entries(data.steps).sort(([a], [b]) => a.localeCompare(b));
  return <>
    <p className="group-note">Latest compile ({new Date(data.updated_at * 1000).toLocaleString()}), steps run under <code>{data.executor}</code>. A hit is a step answered from the cache.</p>
    <table className="metric-table">
      <thead><tr><th>Step</th><th className="num">Cache hits</th><th className="num">Executed</th><th className="num">Seconds</th></tr></thead>
      <tbody>
        {rows.map(([name, m]) => <tr key={name}><td><code>{name}</code></td><td className="num">{m.hits}</td><td className="num">{m.executed}</td><td className="num">{m.seconds.toFixed(2)}</td></tr>)}
        <tr><td><strong>Total</strong></td><td className="num">{data.totals.hits}</td><td className="num">{data.totals.executed}</td><td className="num">{data.totals.seconds.toFixed(2)}</td></tr>
      </tbody>
    </table>
  </>;
}

/** Typed confirmation before something that cannot be undone. */
function ConfirmDelete({ title, body, expected, label, onConfirm, onClose }: { title: string; body: string; expected: string | null; label: string; onConfirm: (typed: string) => Promise<void>; onClose: () => void }) {
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  async function go() {
    setBusy(true);
    setProblem(null);
    try {
      await onConfirm(typed);
    } catch (err) {
      setProblem(err instanceof ApiError ? err.message : "The request failed.");
      setBusy(false);
    }
  }
  return <Modal title={title} onClose={onClose}>
    <p>{body}</p>
    <label className="field"><span>{label}</span><input value={typed} onChange={(e) => setTyped(e.target.value)} data-autofocus autoComplete="off"
      onKeyDown={(e) => { if (e.key === "Enter") e.preventDefault(); /* this dialog sits inside the settings form; Enter must not save it */ }} /></label>
    {problem && <p className="form-error" role="alert">{problem}</p>}
    <div className="form-actions">
      <button type="button" className="btn btn-quiet" onClick={onClose} disabled={busy}>Cancel</button>
      <button type="button" className="btn btn-danger" disabled={busy || !typed || (expected !== null && typed !== expected)} onClick={() => void go()}>{busy ? "Deleting…" : title}</button>
    </div>
  </Modal>;
}

export function Maintenance({ view, setStatus, onChanged }: { view: SettingsResponse; setStatus: Dispatch<SetStateAction<Status | undefined>>; onChanged: () => void }) {
  const [running, setRunning] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<{ id: string; name: string } | "workspace" | null>(null);

  async function run(kind: "gc" | "secret-rescan") {
    setRunning(kind);
    try {
      if (kind === "gc") {
        const r = await api<{ dropped_snapshots: number; objects_deleted: number; webhook_deliveries_pruned?: number }>("maintenance/gc", { body: {} });
        setStatus({ ok: true, text: `Garbage collection removed ${r.dropped_snapshots} snapshots and ${r.objects_deleted} stored objects${r.webhook_deliveries_pruned ? `, and forgot ${r.webhook_deliveries_pruned} old webhook deliveries` : ""}.` });
      } else {
        const r = await api<{ retracted: string[] }>("maintenance/secret-rescan", { body: {} });
        setStatus({ ok: true, text: r.retracted.length ? `The re-scan found new secrets and retracted ${r.retracted.length} snapshots; their refs are recompiling.` : "The re-scan found no new secrets in published snapshots." });
      }
    } catch (err) {
      setStatus({ ok: false, text: err instanceof ApiError ? err.message : "The maintenance run failed." });
    } finally {
      setRunning(null);
    }
  }

  return <section className="work-tool-panel"><h3>Maintenance</h3>
    <h4>Build cache</h4>
    <StepMetricsTable />
    <div className="work-tool-actions">
      <button type="button" className="btn btn-quiet" disabled={running !== null} onClick={() => void run("gc")}>{running === "gc" ? "Collecting…" : "Run garbage collection"}</button>
      <button type="button" className="btn btn-quiet" disabled={running !== null} onClick={() => void run("secret-rescan")}>{running === "secret-rescan" ? "Scanning…" : "Re-scan for secrets"}</button>
    </div>
    <h4>Delete</h4>
    <div className="danger-zone">
      <p className="group-note">Deleting a repository removes its snapshots, lineage, refs, mirror and business-map placements from every store. An audit row with hashes and counts is kept. This cannot be undone.</p>
      {view.repositories.map((repo) => <div className="override-row" key={repo.repository_id}>
        <span>{repo.full_name}</span>
        <button type="button" className="btn btn-quiet btn-small" onClick={() => setDeleting({ id: repo.repository_id, name: repo.full_name })}><Trash2 size={13} aria-hidden="true" /> Delete repository…</button>
      </div>)}
      <div className="override-row"><span>Everything in this workspace</span>
        <button type="button" className="btn btn-quiet btn-small" onClick={() => setDeleting("workspace")}><Trash2 size={13} aria-hidden="true" /> Delete workspace…</button>
      </div>
    </div>
    {deleting && deleting !== "workspace" && <ConfirmDelete title="Delete repository" expected={deleting.name} label={`Type ${deleting.name} to confirm`}
      body={`This deletes ${deleting.name} and everything compiled from it.`} onClose={() => setDeleting(null)}
      onConfirm={async () => { await api(`repositories/${deleting.id}`, { method: "DELETE" }); setDeleting(null); setStatus({ ok: true, text: `${deleting.name} was deleted.` }); onChanged(); }} />}
    {deleting === "workspace" && <ConfirmDelete title="Delete workspace" expected={null} label="Type the workspace's tenant ID to confirm"
      body="This deletes every repository, snapshot, setting and business map in the workspace. Only an audit row remains." onClose={() => setDeleting(null)}
      onConfirm={async (typed) => { await api(`workspace?confirm=${encodeURIComponent(typed)}`, { method: "DELETE" }); setDeleting(null); setStatus({ ok: true, text: "The workspace was deleted." }); onChanged(); }} />}
  </section>;
}
