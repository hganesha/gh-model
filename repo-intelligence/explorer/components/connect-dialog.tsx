"use client";

import { Check, Circle, Loader2, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { api, ApiError, eventsUrl, useApi } from "@/lib/api";
import { shortSha } from "@/lib/format";
import type { Job, JobEvent, Repository } from "@/lib/types";
import type { SettingsView } from "@/lib/workspace";
import { Modal } from "./ui";

const STEPS: { id: string; label: string; stages: string[] }[] = [
  { id: "fetch", label: "Fetch", stages: ["fetch"] },
  { id: "snapshot", label: "Snapshot", stages: ["snapshot"] },
  { id: "extract", label: "Extract", stages: ["extract"] },
  { id: "link", label: "Link & compile", stages: ["link_and_compile", "repo_compile"] },
  { id: "publish", label: "Publish", stages: ["published"] },
  { id: "project", label: "Project", stages: ["project"] },
];

function stepIndex(stage: string): number {
  if (stage === "complete") return STEPS.length;
  return STEPS.findIndex((s) => s.stages.includes(stage));
}

function stepDetail(stepId: string, latest: Record<string, JobEvent>): string | null {
  const n = (v: number | undefined) => (typeof v === "number" ? v.toLocaleString("en-US") : "?");
  switch (stepId) {
    case "snapshot": {
      const e = latest.snapshot;
      return e ? `${n(e.files)} files, ${n(e.excluded)} excluded` : null;
    }
    case "extract": {
      const e = latest.extract;
      return e && e.total !== undefined ? `${n(e.done)} of ${n(e.total)}` : null;
    }
    case "link": {
      const e = latest.link_and_compile;
      if (latest.repo_compile && e) return `${n(e.total)} units, then repository`;
      return e && e.total !== undefined ? `unit ${n(e.done)} of ${n(e.total)}${e.unit ? ` · ${e.unit}` : ""}` : null;
    }
    case "publish": {
      const e = latest.published;
      return e ? `${n(e.steps_executed)} steps run, ${n(e.steps_reused)} reused` : null;
    }
    default:
      return null;
  }
}

/** Follow a compilation over server-sent events, falling back to polling if the stream drops. */
function useJobProgress(jobId: string | null, onSettled: (job: Job) => void) {
  const [events, setEvents] = useState<JobEvent[]>([]);
  const [job, setJob] = useState<Job | null>(null);
  const [streamError, setStreamError] = useState<string | null>(null);
  const settled = useRef(onSettled);
  settled.current = onSettled;

  useEffect(() => {
    if (!jobId) return;
    setEvents([]);
    setJob(null);
    setStreamError(null);
    let closed = false;
    let pollTimer: ReturnType<typeof setTimeout> | undefined;
    const seen = new Set<number>();

    const add = (incoming: JobEvent[]) => {
      const fresh = incoming.filter((e) => typeof e?.seq === "number" && !seen.has(e.seq));
      if (!fresh.length) return;
      fresh.forEach((e) => seen.add(e.seq));
      setEvents((prev) => [...prev, ...fresh].sort((a, b) => a.seq - b.seq));
    };
    const finish = (final: Job) => {
      if (closed) return;
      closed = true;
      setJob(final);
      settled.current(final);
    };

    const poll = async () => {
      if (closed) return;
      try {
        const current = await api<Job>(`compilations/${encodeURIComponent(jobId)}`);
        add(current.events ?? []);
        setStreamError(null);
        if (current.status === "complete" || current.status === "failed") return finish(current);
      } catch (err) {
        setStreamError(err instanceof ApiError ? err.message : "Progress could not be read.");
      }
      pollTimer = setTimeout(poll, 1200);
    };

    const source = new EventSource(eventsUrl(jobId));
    source.onmessage = (e) => {
      try {
        add([JSON.parse(e.data) as JobEvent]);
      } catch {
        /* ignore a malformed frame */
      }
    };
    source.addEventListener("done", (e) => {
      source.close();
      try {
        finish(JSON.parse((e as MessageEvent).data) as Job);
      } catch {
        void poll();
      }
    });
    source.onerror = () => {
      source.close();
      if (!closed) void poll();
    };

    return () => {
      closed = true;
      source.close();
      if (pollTimer) clearTimeout(pollTimer);
    };
  }, [jobId]);

  return { events, job, streamError };
}

function Progress({ events, job, streamError }: { events: JobEvent[]; job: Job | null; streamError: string | null }) {
  const latest: Record<string, JobEvent> = {};
  let reached = -1;
  for (const e of events) {
    latest[e.stage] = e;
    reached = Math.max(reached, stepIndex(e.stage));
  }
  const failed = job?.status === "failed" || !!latest.failed;
  const complete = job?.status === "complete";
  if (complete) reached = STEPS.length;
  const problems = latest.failed?.problems ?? [];

  return (
    <div className="progress">
      <ol className="steps" aria-label="Compilation progress">
        {STEPS.map((step, i) => {
          const state = i < reached ? "done" : i === reached ? (failed ? "failed" : "active") : reached === -1 && i === 0 && !failed ? "active" : "pending";
          const detail = stepDetail(step.id, latest);
          return (
            <li key={step.id} className={`step step-${state}`} aria-current={state === "active" ? "step" : undefined}>
              <span className="step-mark" aria-hidden="true">
                {state === "done" ? <Check size={12} /> : state === "active" ? <Loader2 size={12} className="spin" /> : state === "failed" ? <X size={12} /> : <Circle size={7} />}
              </span>
              <span className="step-label">{step.label}</span>
              {detail && <span className="step-detail">{detail}</span>}
              <span className="sr-only">{state}</span>
            </li>
          );
        })}
      </ol>
      <div aria-live="polite">
        {streamError && !job && <p className="note note-warn">{streamError} Retrying…</p>}
        {complete && (
          <p className="note note-good">
            Snapshot published{job?.commit_sha ? ` for commit ${shortSha(job.commit_sha)}` : ""}. It is now selected.
          </p>
        )}
        {failed && (
          <div className="failure" role="alert">
            <strong>Compilation failed</strong>
            <p>{job?.error ?? "The server reported a failure."}</p>
            {problems.length > 0 && (
              <ul className="mono-list">
                {problems.map((p, i) => (
                  <li key={i}>{p}</li>
                ))}
              </ul>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

export type ConnectTarget = { mode: "connect" } | { mode: "recompile"; repository: Repository; ref: string | null };

export function ConnectDialog({ target, onClose, onSnapshot }: { target: ConnectTarget; onClose: () => void; onSnapshot: (job: Job) => void }) {
  const recompile = target.mode === "recompile";
  const [url, setUrl] = useState("");
  const [connection, setConnection] = useState("github-cli");
  const [page, setPage] = useState(0);
  const configuration = useApi<SettingsView>(recompile ? null : "settings");
  const browse = useApi<{ repositories: { selector: string; full_name: string; default_branch: string }[]; has_more: boolean }>(
    page ? `settings/connections/${connection}/repositories?page=${page}` : null,
  );
  const plain = configuration.data?.settings.connections.find((c) => c.id === connection)?.mode.startsWith("git_") ?? false;
  const [ref, setRef] = useState(recompile ? (target.ref ?? "") : "");
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [jobId, setJobId] = useState<string | null>(null);
  const doneButton = useRef<HTMLButtonElement>(null);

  const { events, job, streamError } = useJobProgress(jobId, (final) => {
    if (final.status === "complete" && final.rkp_hash) onSnapshot(final);
    doneButton.current?.focus();
  });

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (submitting) return;
    setSubmitting(true);
    setSubmitError(null);
    const trimmedRef = ref.trim();
    try {
      const started = recompile
        ? // repository_id contains a slash that must stay a path separator
          await api<Job>(`repositories/${target.repository.repository_id}/compilations`, { body: trimmedRef ? { ref: trimmedRef } : {} })
        : await api<Job>("repositories", { body: { url: url.trim(), ref: trimmedRef || undefined, connection_id: connection } });
      setJobId(started.id);
    } catch (err) {
      setSubmitError(err instanceof ApiError ? err.message : "The request could not be sent.");
    } finally {
      setSubmitting(false);
    }
  }

  const title = recompile ? "Recompile repository" : "Connect repository";
  const settled = job?.status === "complete" || job?.status === "failed";

  return (
    <Modal title={title} onClose={onClose} className="connect-modal work-tool-dialog">
      {jobId === null ? (
        <form onSubmit={submit} className="form">
          {recompile ? (
            <p className="form-lead">
              Compile a fresh snapshot of <strong>{target.repository.full_name}</strong>. Existing snapshots are kept.
            </p>
          ) : (
            <>
              <label className="field">
                <span>Connection</span>
                <select value={connection} onChange={(e) => { setConnection(e.target.value); setPage(0); setUrl(""); setRef(""); }}>
                  <option value="github-cli">GitHub CLI</option>
                  {configuration.data?.settings.connections.map((row) => <option key={row.id} value={row.id}>{row.name} · {row.host}</option>)}
                </select>
              </label>
              <button type="button" className="btn btn-quiet" onClick={() => page ? browse.reload() : setPage(1)}>Browse repositories</button>
              {browse.loading && <p role="status">Loading repositories…</p>}
              {browse.error && <p className="form-error" role="alert">{browse.error.message}</p>}
              {browse.data && <div className="work-tool-controls">
                <label className="field"><span>Visible repositories</span><select value={url} onChange={(e) => {
                  setUrl(e.target.value);
                  setRef(browse.data?.repositories.find((r) => r.selector === e.target.value)?.default_branch ?? "");
                }}>
                  <option value="">Choose a repository</option>
                  {browse.data.repositories.map((row) => <option key={row.selector} value={row.selector}>{row.full_name}</option>)}
                </select></label>
                {page > 1 && <button type="button" className="btn btn-quiet" onClick={() => setPage(page - 1)}>Previous</button>}
                {browse.data.has_more && <button type="button" className="btn btn-quiet" onClick={() => setPage(page + 1)}>Next</button>}
              </div>}
              {!plain && <label className="field">
                <span>Repository URL or name</span>
                <input data-autofocus value={url} onChange={(e) => setUrl(e.target.value)} placeholder="owner/name" required autoComplete="off" spellCheck={false} autoCapitalize="none" />
              </label>}
            </>
          )}
          <label className="field">
            <span>
              Ref <i>optional</i>
            </span>
            <input data-autofocus={recompile ? true : undefined} value={ref} onChange={(e) => setRef(e.target.value)} placeholder="branch, tag or commit — defaults to the default branch" autoComplete="off" spellCheck={false} autoCapitalize="none" />
          </label>
          {submitError && (
            <p className="form-error" role="alert">
              {submitError}
            </p>
          )}
          <div className="form-actions">
            <button type="button" className="btn btn-quiet" onClick={onClose}>
              Cancel
            </button>
            <button type="submit" className="btn btn-primary" disabled={submitting || (!recompile && !plain && !url.trim())}>
              {submitting && <Loader2 size={14} className="spin" aria-hidden="true" />}
              {recompile ? "Recompile" : "Connect and compile"}
            </button>
          </div>
        </form>
      ) : (
        <div className="form">
          <Progress events={events} job={job} streamError={streamError} />
          <div className="form-actions">
            {job?.status === "failed" && (
              <button type="button" className="btn btn-quiet" onClick={() => setJobId(null)}>
                Change and retry
              </button>
            )}
            <button ref={doneButton} type="button" className={`btn ${settled ? "btn-primary" : "btn-quiet"}`} onClick={onClose}>
              {settled ? "Done" : "Close — compilation continues"}
            </button>
          </div>
        </div>
      )}
    </Modal>
  );
}
