"use client";

import { Search } from "lucide-react";
import { useMemo, useState } from "react";
import { api, ApiError, snapshotPath, useApi } from "@/lib/api";
import {
  countByDocument,
  designation,
  FEATURES,
  isReadable,
  otherEntries,
  problemText,
  PRODUCED_KINDS,
  producingPaths,
  sameEntries,
  saveBody,
  saveFailureText,
  toggle,
  unreadableDocuments,
  type DocumentCounts,
  type DocumentEntry,
  type DocumentsResponse,
  type Feature,
} from "@/lib/documents";
import { domainLabel } from "@/lib/planes";
import { plural } from "@/lib/format";
import type { EntitiesResponse, Summary } from "@/lib/types";
import { Badge, Coverage, ErrorNote, Loading, Note } from "./ui";

/** Up to this many entities of each produced kind are read to count them per document. */
const COUNT_LIMIT = 500;

/** The snapshot's Requirements, BusinessRules, DataSchemas and CodeLists, counted per document. */
function useProduced(hash: string, summary: Summary) {
  const path = (kind: string) => ((summary.counts_by_kind[kind] ?? 0) > 0 ? snapshotPath(hash, `entities?${new URLSearchParams({ kind, limit: String(COUNT_LIMIT) })}`) : null);
  // One request per kind, in the fixed order of PRODUCED_KINDS; a kind the summary counted as zero is not fetched.
  const requirements = useApi<EntitiesResponse>(path("Requirement"));
  const rules = useApi<EntitiesResponse>(path("BusinessRule"));
  const schemas = useApi<EntitiesResponse>(path("DataSchema"));
  const codeLists = useApi<EntitiesResponse>(path("CodeList"));
  const states = [requirements, rules, schemas, codeLists];
  const entities = useMemo(() => states.flatMap((s) => s.data?.entities ?? []),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [requirements.data, rules.data, schemas.data, codeLists.data]);
  return {
    entities,
    loading: states.some((s) => s.loading),
    error: states.find((s) => s.error)?.error,
    truncated: states.some((s) => s.data?.truncated),
    reload: () => states.forEach((s) => s.reload()),
  };
}

function ProducedCounts({ counts, designated }: { counts: DocumentCounts[string] | undefined; designated: boolean }) {
  const parts = PRODUCED_KINDS.filter((k) => (counts?.[k.kind] ?? 0) > 0).map((k) => `${counts![k.kind]} ${k.label.toLowerCase()}`);
  if (parts.length) return <span>{parts.join(" · ")}</span>;
  return <span className="muted">{designated ? "none in this snapshot" : "—"}</span>;
}

function FeatureCheck({ path, feature, own, inherited, disabled, onChange }: {
  path: string;
  feature: (typeof FEATURES)[number];
  own: readonly Feature[];
  inherited: { path: string; features: Feature[] }[];
  disabled: boolean;
  onChange: (on: boolean) => void;
}) {
  const from = inherited.filter((g) => g.features.includes(feature.id)).map((g) => g.path);
  const checked = own.includes(feature.id) || from.length > 0;
  // A feature a glob gives cannot be turned off for one file here: the glob entry is edited below.
  const locked = !own.includes(feature.id) && from.length > 0;
  return (
    <td className="check">
      <input
        type="checkbox"
        checked={checked}
        disabled={disabled || locked}
        onChange={(e) => onChange(e.target.checked)}
        aria-label={`Use ${path} for ${feature.label.toLowerCase()}`}
        title={locked ? `Designated by ${from.join(", ")}` : feature.hint}
      />
    </td>
  );
}

export function DocumentsView({ hash, summary, repositoryId }: { hash: string; summary: Summary; repositoryId: string | null }) {
  const listing = useApi<DocumentsResponse>(repositoryId ? `repositories/${repositoryId}/documents` : null);
  const produced = useProduced(hash, summary);
  const [draft, setDraft] = useState<{ base: DocumentsResponse; entries: DocumentEntry[] } | null>(null);
  const [text, setText] = useState("");
  const [designatedOnly, setDesignatedOnly] = useState(false);
  const [saving, setSaving] = useState(false);
  const [status, setStatus] = useState<{ ok: boolean; text: string; stale?: boolean } | null>(null);

  // An engine from before designated documents answers 404: the snapshot's counts are still shown.
  const unavailable = listing.error instanceof ApiError && listing.error.status === 404;
  const data = listing.data;
  const saved = data?.documents ?? [];
  const entries = draft && draft.base === data ? draft.entries : saved;
  const changed = !!data && !sameEntries(entries, saved);

  const candidates = useMemo(() => {
    if (data) return data.candidates;
    // Without the candidate list, the documents the snapshot has results from.
    return producingPaths(produced.entities).map((path) => ({ path, language: "" }));
  }, [data, produced.entities]);
  const paths = useMemo(() => new Set(candidates.map((c) => c.path)), [candidates]);
  const counts = useMemo(() => countByDocument(produced.entities, paths), [produced.entities, paths]);
  const unreadable = useMemo(() => new Map(unreadableDocuments(summary.coverage).map((u) => [u.path, u.needs])), [summary.coverage]);
  const others = useMemo(() => (data ? otherEntries(entries, data.candidates) : []), [data, entries]);

  const rows = candidates
    .map((c) => ({ ...c, ...designation(entries, c.path) }))
    .filter((r) => !text || r.path.toLowerCase().includes(text.toLowerCase()))
    .filter((r) => !designatedOnly || r.own.length > 0 || r.inherited.length > 0 || counts[r.path]);
  const designatedCount = candidates.filter((c) => {
    const d = designation(entries, c.path);
    return d.own.length > 0 || d.inherited.length > 0;
  }).length;

  function edit(next: DocumentEntry[]) {
    if (!data) return;
    setDraft({ base: data, entries: next });
    setStatus(null);
  }

  async function save() {
    if (!repositoryId || !data) return;
    setSaving(true);
    setStatus(null);
    try {
      await api(`repositories/${repositoryId}/documents`, { method: "PUT", body: saveBody(entries, data) });
      setDraft(null);
      listing.reload();
      setStatus({ ok: true, text: "Saved to .rkp/documents.json. It takes effect at the next compile: recompile to see the results." });
    } catch (err) {
      // On 409 the file changed since it was read and the engine wrote nothing. The draft stays
      // on screen until the owner reloads, so this save never overwrites the newer file.
      const known = err instanceof ApiError ? err : undefined;
      setStatus({ ok: false, stale: known?.status === 409, text: saveFailureText(known?.status, known?.message) });
    } finally {
      setSaving(false);
    }
  }

  const coverage = summary.coverage_totals?.documents;

  return (
    <section className="decisions-view documents-view" aria-label="Documents">
      <header className="lead">
        <span className="eyebrow">DESIGNATED DOCUMENTS</span>
        <h2>Documents</h2>
        <p>Choose which documents the engine reads, and for what: requirements, business rules, data schemas or code lists. Nothing undesignated is read for these features. The choices are saved to <code>.rkp/documents.json</code> in the repository and take effect at the next compile.</p>
      </header>

      {coverage && <Coverage label={domainLabel("documents")} ratio={coverage} />}

      {!repositoryId && <Note>Select a repository to designate its documents.</Note>}
      {listing.loading && <Loading label="Loading documents" />}
      {unavailable && (
        <Note tone="warn">
          This engine does not serve designated documents (it answered 404 for <code>repositories/…/documents</code>), so they cannot be changed here. Update the engine, or edit <code>.rkp/documents.json</code> in the repository. The counts below are what this snapshot holds.
        </Note>
      )}
      {listing.error && !unavailable && <ErrorNote error={listing.error} onRetry={listing.reload} what="the designated documents" />}
      {produced.error && <ErrorNote error={produced.error} onRetry={produced.reload} what="the entities documents produced" />}
      {data && data.problems.length > 0 && (
        <section className="panel">
          <h3>Problems in .rkp/documents.json <span className="section-count">{data.problems.length}</span></h3>
          <p className="group-note">An entry with an unknown feature, or a path that matches no file, is ignored at compile.</p>
          <ul className="lineage-list">{data.problems.map((p, i) => <li key={i}>{problemText(p)}</li>)}</ul>
        </section>
      )}

      {(data || unavailable) && (
        <section className="panel">
          <h3>
            {plural(candidates.length, "document")}
            {data && <span className="section-count">{designatedCount} designated</span>}
          </h3>
          {candidates.length > 0 && (
            <div className="filters">
              <label className="text-filter">
                <Search size={14} aria-hidden="true" />
                <span className="sr-only">Filter documents by path</span>
                <input type="search" value={text} onChange={(e) => setText(e.target.value)} placeholder="Filter by path" autoComplete="off" spellCheck={false} />
              </label>
              <label className="work-check"><input type="checkbox" checked={designatedOnly} onChange={(e) => setDesignatedOnly(e.target.checked)} /> Designated or producing only</label>
            </div>
          )}
          {candidates.length === 0 && (
            <p className="group-note">{data ? "This snapshot has no Markdown, text, CSV, HTML, DOCX, XLSX or PDF documents." : "This snapshot has no results from designated documents."}</p>
          )}
          {rows.length > 0 && (
            <div className="table-scroll">
              <table className="data-table documents-table">
                <thead>
                  <tr>
                    <th scope="col">Document</th>
                    {data && FEATURES.map((f) => <th scope="col" key={f.id} className="check" title={f.hint}>{f.label}</th>)}
                    <th scope="col">Produced</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r) => {
                    const designated = r.own.length > 0 || r.inherited.length > 0;
                    const needs = unreadable.get(r.path);
                    return (
                      <tr key={r.path}>
                        <th scope="row" className="document-path">
                          <code>{r.path}</code>
                          {r.language && <span className="muted"> {r.language}</span>}
                          {designated && (unreadable.has(r.path) || (r.language && !isReadable(r.language))) && (
                            <Badge tone="candidate" title="The engine cannot read this format's text">needs {needs ?? "a Markdown, DOCX or text export"}</Badge>
                          )}
                        </th>
                        {data && FEATURES.map((f) => (
                          <FeatureCheck key={f.id} path={r.path} feature={f} own={r.own} inherited={r.inherited} disabled={saving}
                            onChange={(on) => edit(toggle(entries, r.path, f.id, on))} />
                        ))}
                        <td><ProducedCounts counts={counts[r.path]} designated={designated} /></td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
          {candidates.length > 0 && rows.length === 0 && <p className="group-note">No document matches the filter.</p>}
          {produced.loading && <Loading label="Counting what documents produced" />}
          {produced.truncated && <p className="group-note">Counted from the first {COUNT_LIMIT} entities of each kind; a document may have produced more.</p>}
        </section>
      )}

      {data && others.length > 0 && (
        <section className="panel">
          <h3>Other entries <span className="section-count">{others.length}</span></h3>
          <p className="group-note">Globs, and paths that name no document in this snapshot. A file matched by several entries is read for all of their features.</p>
          <ul className="lineage-list">
            {others.map((e) => (
              <li key={e.path}>
                <code>{e.path}</code> {e.features.map((f) => <Badge key={f}>{f.replaceAll("_", " ")}</Badge>)}
                {e.note && <span className="muted">{e.note}</span>}
                <button type="button" className="link-btn" disabled={saving} onClick={() => edit(entries.filter((x) => x.path !== e.path))}>Remove</button>
              </li>
            ))}
          </ul>
        </section>
      )}

      {data && (
        <div className="documents-actions">
          <button type="button" className="btn btn-primary btn-small" disabled={!changed || saving} onClick={() => void save()}>{saving ? "Saving…" : "Save designations"}</button>
          <button type="button" className="btn btn-quiet btn-small" disabled={!changed || saving} onClick={() => { setDraft(null); setStatus(null); }}>Discard changes</button>
          {changed && !status && <span className="muted">Unsaved changes.</span>}
          {entries.some((e) => e.features.includes("rules")) && <span className="muted">Rules are named by a model only when the business rules setting is on; it sends the document&apos;s text to the configured model provider.</span>}
        </div>
      )}
      {status && (
        <p className={status.ok ? "group-note" : "form-error"} role={status.ok ? "status" : "alert"}>
          {status.text}
          {status.stale && (
            <> <button type="button" className="link-btn" onClick={() => { setDraft(null); setStatus(null); listing.reload(); }}>Reload</button></>
          )}
        </p>
      )}
    </section>
  );
}
