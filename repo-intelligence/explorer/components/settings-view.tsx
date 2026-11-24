"use client";

import { Plus, Trash2 } from "lucide-react";
import { useState, type Dispatch, type SetStateAction } from "react";
import { api, snapshotPath, useApi } from "@/lib/api";
import type { Summary } from "@/lib/types";
import type { Connection, ConnectionMode, ModelSelection, SettingsDocument, SettingsView as SettingsResponse, Tracking } from "@/lib/workspace";
import { Badge, ErrorNote, Loading } from "./ui";

const modes: Record<ConnectionMode, string> = {
  github_pat: "GitHub token", github_device: "GitHub device sign-in", github_app: "GitHub App",
  gitlab_token: "GitLab token", git_https: "Git HTTPS token", git_ssh: "Git SSH deploy key",
};
const providers = ["jev", "microsoft-decision", "azure-openai", "cohere-rerank", "openai-embeddings"];
const kinds = ["service", "worker", "frontend_app", "library", "cli", "tooling", "unknown"];
const policyDefault: Tracking = { refs: [], trigger: "manual", interval_minutes: 15 };

function defaultConnection(): Connection {
  return {
    id: crypto.randomUUID(), name: "New connection", mode: "github_pat", host: "github.com",
    credential_ref: null, webhook_ref: null, app_id: null, installation_id: null, remote: null, client_id: null,
  };
}
function toggle<T>(values: T[], item: T, on: boolean): T[] {
  return on ? [...new Set([...values, item])] : values.filter((v) => v !== item);
}

function ConnectionEditor({ value, onChange, store }: { value: Connection; onChange: (row: Connection) => void; store: "vault" | "keychain" }) {
  function field(key: keyof Connection, raw: string) {
    onChange({ ...value, [key]: raw || null });
  }

  function mode(next: ConnectionMode) {
    onChange({
      ...value, mode: next, host: next.startsWith("github_") ? "github.com" : next === "gitlab_token" ? "gitlab.com" : "",
      remote: null, app_id: null, installation_id: null, client_id: null, webhook_ref: null, credential_ref: null,
    });
  }
  const github = value.mode.startsWith("github_");
  return <div className="work-tool-controls">
    <label className="field"><span>Name</span><input required value={value.name} onChange={(e) => onChange({ ...value, name: e.target.value })} /></label>
    <label className="field"><span>Authentication</span><select value={value.mode} onChange={(e) => mode(e.target.value as ConnectionMode)}>{Object.entries(modes).map(([id, label]) => <option value={id} key={id}>{label}</option>)}</select></label>
    <label className="field"><span>Host</span><input value={value.host} disabled={github} required onChange={(e) => onChange({ ...value, host: e.target.value })} /></label>
    <label className="field"><span>Credential reference</span><input autoComplete="off" value={value.credential_ref ?? ""} required={value.mode !== "github_device"}
      placeholder={value.mode === "github_device" ? `Optional: kept in ${store === "vault" ? "Key Vault" : "the OS keychain"}` : `${store}:my-connection`} onChange={(e) => field("credential_ref", e.target.value)} /></label>
    {value.mode === "github_device" && <label className="field"><span>OAuth client ID</span><input required value={value.client_id ?? ""} onChange={(e) => field("client_id", e.target.value)} /></label>}
    {value.mode.startsWith("git_") && <label className="field"><span>Remote URL</span><input required value={value.remote ?? ""} onChange={(e) => field("remote", e.target.value)} /></label>}
    {value.mode === "github_app" && <>
      <label className="field"><span>App ID</span><input required value={value.app_id ?? ""} onChange={(e) => field("app_id", e.target.value)} /></label>
      <label className="field"><span>Installation ID</span><input required value={value.installation_id ?? ""} onChange={(e) => field("installation_id", e.target.value)} /></label>
    </>}
    {(value.mode === "github_app" || value.mode === "gitlab_token") && <label className="field"><span>Webhook secret reference</span><input value={value.webhook_ref ?? ""} autoComplete="off" placeholder="managed:repo-webhook" onChange={(e) => field("webhook_ref", e.target.value)} /></label>}
  </div>;
}

type Status = { ok: boolean; text: string };
function Connections({ view, draft, update, busy, setStatus }: {
  view: SettingsResponse; draft: SettingsDocument; update: Dispatch<SetStateAction<SettingsDocument>>;
  busy: boolean; setStatus: Dispatch<SetStateAction<Status | undefined>>;
}) {
  const [checks, setChecks] = useState(view.checks);
  const [waiting, setWaiting] = useState<string>();
  const [devices, setDevices] = useState<Record<string, { user_code: string; verification_uri: string }>>({});
  const persisted = new Map(view.settings.connections.map((row) => [row.id, row]));
  async function check(id: string) {
    setWaiting(id);
    try {
      const status = await api<SettingsResponse["checks"][string]>(`settings/connections/${id}/test`, { body: {} });
      setChecks((prev) => ({ ...prev, [id]: status }));
    } catch (failure) {
      setStatus({ ok: false, text: failure instanceof Error ? failure.message : "The connection check failed." });
    } finally {
      setWaiting(undefined);
    }
  }
  async function device(id: string, poll: boolean) {
    setWaiting(id);
    try {
      if (poll) {
        const result = await api<{ status: string }>(`settings/connections/${id}/device/poll`, { body: {} });
        setStatus({ ok: result.status === "complete", text: result.status === "complete" ? "GitHub authorization saved to the OS keychain." : `Authorization is ${result.status}.` });
        if (result.status !== "pending") {
          setDevices((prev) => {
            const next = { ...prev };
            delete next[id];
            return next;
          });
        }
      } else {
        const code = await api<{ user_code: string; verification_uri: string }>(`settings/connections/${id}/device`, { body: {} });
        setDevices((prev) => ({ ...prev, [id]: code }));
      }
    } catch (failure) {
      setStatus({ ok: false, text: failure instanceof Error ? failure.message : "GitHub sign-in failed." });
    } finally {
      setWaiting(undefined);
    }
  }
  function info(id: string) {
    const check = checks[id];
    if (!check) return <Badge tone="neutral">Unchecked</Badge>;
    return <p className="group-note"><Badge tone={check.status === "connected" ? "verified" : "candidate"}>{check.status}</Badge>{" "}
      {new Date(check.checked_at * 1000).toLocaleString()} · {check.repositories_sampled === undefined ? check.message : `${check.repositories_sampled} repositories on the first page`}
      {check.scopes?.length ? ` · ${check.scopes.join(", ")}` : ""}</p>;
  }
  const store = view.credential_store ?? "keychain";
  const hosted = store === "vault";
  return <section className="work-tool-panel">
    <h3>Connections</h3>
    {hosted
      ? <p className="group-note">This server keeps credentials in Azure Key Vault (<code>vault:</code>) or a mounted secret (<code>managed:</code>). To sign in to GitHub from here, add a connection with GitHub device sign-in and your GitHub OAuth App&apos;s client ID, save, then start device sign-in.</p>
      : <p className="group-note">Store credentials in the OS keychain or a mounted secret manager. To rotate a credential, update its value there or save a new reference here.</p>}
    {view.github_cli !== false && <div className="work-tool-row"><strong>GitHub CLI · github.com</strong>{info("github-cli")}
      <button type="button" className="btn btn-quiet btn-small" disabled={Boolean(waiting)} onClick={() => void check("github-cli")}>Test connection</button>
    </div>}
    {draft.connections.map((row) => {
      const saved = JSON.stringify(row) === JSON.stringify(persisted.get(row.id));
      const webhook = view.last_webhook[row.id];
      return <div className="work-tool-row" key={row.id}>
        <ConnectionEditor value={row} store={store} onChange={(next) => update((prev) => ({ ...prev, connections: prev.connections.map((item) => item.id === next.id ? next : item) }))} />
        {info(row.id)}
        {row.webhook_ref && <p className="group-note">Webhook {webhook ? `last received ${new Date(Number(webhook) * 1000).toLocaleString()}` : "awaiting its first tracked push"} · <code>{`/v1/webhooks/${row.id}`}</code></p>}
        {devices[row.id] && <p className="note">
          <a href={devices[row.id].verification_uri} target="_blank" rel="noreferrer">Open GitHub device authorization</a> and enter <code>{devices[row.id].user_code}</code>.
        </p>}
        <div className="work-tool-actions">
          <button type="button" className="btn btn-quiet btn-small" disabled={Boolean(waiting) || !saved || busy} onClick={() => void check(row.id)}>Test connection</button>
          {row.mode === "github_device" && <button type="button" className="btn btn-quiet btn-small" disabled={!saved || Boolean(waiting)} onClick={() => void device(row.id, Boolean(devices[row.id]))}>{devices[row.id] ? "Check authorization" : "Start device sign-in"}</button>}
          <button type="button" className="btn btn-quiet btn-small" onClick={() => update((prev) => ({ ...prev, connections: prev.connections.filter((c) => c.id !== row.id) }))}><Trash2 size={14} /> Remove</button>
        </div>
      </div>;
    })}
    <button type="button" className="btn btn-quiet" onClick={() => update((prev) => ({ ...prev, connections: [...prev.connections, defaultConnection()] }))}><Plus size={14} /> Add connection</button>
  </section>;
}

function RepositorySettings({ view, draft, update }: { view: SettingsResponse; draft: SettingsDocument; update: Dispatch<SetStateAction<SettingsDocument>> }) {
  function patch(id: string, change: Partial<Tracking>) {
    update((prev) => ({ ...prev, repositories: { ...prev.repositories, [id]: { ...(prev.repositories[id] ?? policyDefault), ...change } } }));
  }
  return <section className="work-tool-panel"><h3>Repositories</h3>
    {!view.repositories.length && <p className="group-note">Connect a repository to configure tracked refs and triggers.</p>}
    {view.repositories.map((repo) => {
      const version = repo.snapshots.at(-1);
      const row = draft.repositories[repo.repository_id] ?? { ...policyDefault, refs: [repo.default_branch] };
      return <div key={repo.repository_id} className="work-tool-row">
        <div className="work-tool-controls"><strong>{repo.full_name}</strong><p>{draft.connections.find((c) => c.id === repo.connection_id)?.name ?? (repo.source === "local" ? "Local Git" : repo.connection_id ?? "GitHub CLI")}</p>
          {version && <p>Last compile {new Date(version.compiled_at * 1000).toLocaleString()} {version.stale && <Badge tone="candidate">Settings changed</Badge>}</p>}
        </div>
        <div className="work-tool-controls">
          <label className="field"><span>Tracked refs for {repo.full_name}</span>
            <input defaultValue={row.refs.join(", ")} onBlur={(e) => patch(repo.repository_id, { refs: e.target.value.split(",").map((ref) => ref.trim()).filter(Boolean) })} />
          </label>
          <label className="field"><span>Compile trigger</span><select value={row.trigger} onChange={(e) => patch(repo.repository_id, { refs: row.refs, trigger: e.target.value as Tracking["trigger"] })}>
            <option value="manual">Manual</option><option value="push">On push</option><option value="poll">Poll</option>
          </select></label>
          {row.trigger === "poll" && <label className="field"><span>Poll interval (minutes)</span><input type="number" value={row.interval_minutes} min={1} max={1440} onChange={(e) => patch(repo.repository_id, { interval_minutes: Number(e.target.value) })} /></label>}
        </div>
      </div>;
    })}
  </section>;
}

function ExtractorSettings({ view, draft, update }: { view: SettingsResponse; draft: SettingsDocument; update: Dispatch<SetStateAction<SettingsDocument>> }) {
  const [repository, setRepository] = useState("");
  const snap = view.repositories.find((r) => r.repository_id === repository)?.snapshots.at(-1);
  const summary = useApi<Summary>(snap ? snapshotPath(snap.rkp_hash, "summary") : null);
  return <section className="work-tool-panel"><h3>Languages and extractors</h3>
    <div className="work-tool-grid">{view.extractors.map((row) => <label key={row.id} className="work-check">
      <input type="checkbox" checked={!draft.compile.disabled_extractors.includes(row.id)} onChange={(e) => update((prev) => ({ ...prev, compile: { ...prev.compile, disabled_extractors: toggle(prev.compile.disabled_extractors, row.id, !e.target.checked) } }))} />
      <span><strong>{row.id}</strong><small>{row.version} · {row.tier === "inventory" ? "Inventory only" : `Tier ${row.tier}`}</small></span>
    </label>)}</div>
    {view.repositories.length > 0 && <label className="field"><span>Measured coverage</span><select value={repository} onChange={(e) => setRepository(e.target.value)}>
      <option value="">Choose a repository</option>{view.repositories.map((r) => <option key={r.repository_id} value={r.repository_id}>{r.full_name}</option>)}
    </select></label>}
    {summary.loading && <Loading label="Loading extraction coverage" />}
    {summary.error && <ErrorNote error={summary.error} onRetry={summary.reload} what="coverage" />}
    {summary.data && <ul className="work-coverage">{Object.entries(summary.data.coverage_totals).map(([key, ratio]) => <li key={key}>{key.replaceAll("_", " ")} <strong>{ratio.numerator}/{ratio.denominator}</strong></li>)}</ul>}
  </section>;
}

function Models({ view, draft, update, setStatus }: {
  view: SettingsResponse; draft: SettingsDocument; update: Dispatch<SetStateAction<SettingsDocument>>;
  setStatus: Dispatch<SetStateAction<Status | undefined>>;
}) {
  const [testing, setTesting] = useState(false);
  const aliases = Object.keys(view.available_models).filter((id) => id !== "decision-default");
  const activeAllow = view.environment.allowed_providers ?? draft.compile.allowed_providers;
  const activeDefault = view.environment.default_model === undefined ? draft.compile.default_model : view.environment.default_model;
  function patch(change: Partial<SettingsDocument["compile"]>) {
    update((prev) => ({ ...prev, compile: { ...prev.compile, ...change } }));
  }
  function choice(id: string, key: keyof ModelSelection, value: string | number | null) {
    update((prev) => ({
      ...prev, compile: { ...prev.compile, model_functions: {
        ...prev.compile.model_functions, [id]: { ...(prev.compile.model_functions[id] ?? { alias: null, threshold: null, margin: null }), [key]: value },
      } },
    }));
  }
  async function check() {
    if (!activeDefault) return;
    setTesting(true);
    try {
      const report = await api<{ results: { abstain_reason: string | null }[] }>(`settings/models/test?alias=${encodeURIComponent(activeDefault)}`, { body: {} });
      const failures = report.results.filter((row) => ["model_unavailable", "egress_denied"].includes(row.abstain_reason ?? "")).length;
      setStatus({ ok: failures === 0, text: `Live check completed ${report.results.length - failures} of ${report.results.length} synthetic cases.` });
    } catch (failure) {
      setStatus({ ok: false, text: failure instanceof Error ? failure.message : "Decision live check failed." });
    } finally {
      setTesting(false);
    }
  }
  return <section className="work-tool-panel"><h3>Decision models</h3>
    <p className="group-note">Allowing a provider enables it to receive bounded structured repository features. Hosted embeddings receive the component document.</p>
    <div className="work-tool-grid">{providers.map((id) => <label key={id} className="work-check"><input type="checkbox" disabled={view.environment.allowed_providers !== undefined} checked={activeAllow.includes(id)}
      onChange={(e) => patch({ allowed_providers: toggle(draft.compile.allowed_providers, id, e.target.checked) })} /> {id} {view.environment.allowed_providers !== undefined && <small>Environment</small>}</label>)}</div>
    <div className="work-tool-controls">
      <label className="field"><span>Default model {view.environment.default_model === undefined ? "" : "· Environment"}</span><select value={activeDefault ?? ""} disabled={view.environment.default_model !== undefined} onChange={(e) => patch({ default_model: e.target.value || null })}>
        <option value="">Rules only</option>{aliases.map((id) => <option key={id} value={id}>{view.available_models[id]}</option>)}
      </select></label>
      <label className="field"><span>Model calls per compile</span><input type="number" min={0} max={2000} value={draft.compile.decision_budget} onChange={(e) => patch({ decision_budget: Number(e.target.value) })} /></label>
      <button type="button" className="btn btn-quiet" disabled={!activeDefault || testing} onClick={() => void check()}>{testing ? "Testing…" : "Test saved model"}</button>
    </div>
    {["dm.component-kind", "dm.capability-placement"].map((id) => <div className="work-tool-row" key={id}><strong>{id}</strong>
      <div className="work-tool-controls">
        <label className="field"><span>Model</span><select value={draft.compile.model_functions[id]?.alias ?? ""} onChange={(e) => choice(id, "alias", e.target.value || null)}>
          <option value="">Workspace default</option>{aliases.map((alias) => <option value={alias} key={alias}>{alias}</option>)}
        </select></label>
        {id === "dm.capability-placement"
          ? <p className="group-note work-tool-control-wide">Acceptance threshold: set once as the placement threshold under Business map below.</p>
          : <label className="field"><span>Acceptance threshold</span><input type="number" step="0.01" min={0} max={1} value={draft.compile.model_functions[id]?.threshold ?? ""} onChange={(e) => choice(id, "threshold", e.target.value === "" ? null : Number(e.target.value))} /></label>}
        <label className="field"><span>Minimum margin</span><input type="number" step="0.01" min={0} max={1} value={draft.compile.model_functions[id]?.margin ?? ""} onChange={(e) => choice(id, "margin", e.target.value === "" ? null : Number(e.target.value))} /></label>
      </div>
    </div>)}
  </section>;
}

function MapPolicy({ draft, update }: { draft: SettingsDocument; update: Dispatch<SetStateAction<SettingsDocument>> }) {
  function patch(change: Partial<SettingsDocument["business_map"]>) {
    update((prev) => ({ ...prev, business_map: { ...prev.business_map, ...change } }));
  }
  return <section className="work-tool-panel"><h3>Business map</h3>
    <label className="work-check"><input type="checkbox" checked={draft.business_map.auto_place} onChange={(e) => patch({ auto_place: e.target.checked })} /> Automatically place accepted components</label>
    <div className="work-tool-controls">
      <label className="field"><span>Placement threshold</span><input type="number" required min={0} max={1} step="0.01" value={draft.business_map.threshold} onChange={(e) => patch({ threshold: Number(e.target.value) })} /></label>
      <label className="field"><span>Embedding provider</span><select value={draft.business_map.embedding_provider} onChange={(e) => patch({ embedding_provider: e.target.value as SettingsDocument["business_map"]["embedding_provider"] })}>
        <option value="local">Local hash · lexical similarity</option><option value="openai-embeddings">OpenAI-compatible</option>
      </select></label>
    </div>
    <div className="work-tool-grid">{kinds.map((kind) => <label className="work-check" key={kind}><input type="checkbox" checked={draft.business_map.component_kinds.includes(kind)} onChange={(e) => patch({ component_kinds: toggle(draft.business_map.component_kinds, kind, e.target.checked) })} /> {kind.replaceAll("_", " ")}</label>)}</div>
  </section>;
}

function SettingsForm({ initial, reload }: { initial: SettingsResponse; reload: () => void }) {
  const [view, setView] = useState(initial);
  const [draft, setDraft] = useState(initial.settings);
  const [status, setStatus] = useState<Status>();
  const [busy, setBusy] = useState(false);
  async function save() {
    setBusy(true);
    try {
      const functions = { ...draft.compile.model_functions };
      if (functions["dm.capability-placement"]) functions["dm.capability-placement"] = { ...functions["dm.capability-placement"], threshold: null };
      const next = await api<SettingsResponse>("settings", { body: { ...draft, compile: { ...draft.compile, model_functions: functions }, expected_version: view.version } });
      setView(next);
      setDraft(next.settings);
      setStatus({ ok: true, text: "Workspace settings saved." });
    } catch (failure) {
      setStatus({ ok: false, text: failure instanceof Error ? failure.message : "Settings could not be saved." });
    } finally {
      setBusy(false);
    }
  }
  return <form className="work-tool" onSubmit={(event) => { event.preventDefault(); void save(); }}>
    <header className="work-tool-header"><div><h2>Settings</h2>
      {view.updated_at && <p>Revision {view.version} · {view.author} · {new Date(view.updated_at).toLocaleString()}</p>}
    </div><div className="work-tool-actions"><button type="button" className="btn btn-quiet" onClick={reload} disabled={busy}>Reload</button><button type="submit" className="btn btn-primary" disabled={busy}>Save settings</button></div></header>
    {status && <p role={status.ok ? "status" : "alert"} className={status.ok ? "note note-good" : "form-error"}>{status.text}</p>}
    <Connections view={view} draft={draft} update={setDraft} busy={busy} setStatus={setStatus} />
    <RepositorySettings view={view} draft={draft} update={setDraft} />
    <ExtractorSettings view={view} draft={draft} update={setDraft} />
    <Models view={view} draft={draft} update={setDraft} setStatus={setStatus} />
    <MapPolicy draft={draft} update={setDraft} />
  </form>;
}

export function SettingsView() {
  const data = useApi<SettingsResponse>("settings");
  if (data.loading) return <Loading label="Loading workspace settings" />;
  if (data.error) return <ErrorNote error={data.error} onRetry={data.reload} what="workspace settings" />;
  return data.data ? <SettingsForm initial={data.data} reload={data.reload} /> : null;
}
