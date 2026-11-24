"use client";

import { Archive, ShieldCheck } from "lucide-react";
import { useState } from "react";
import { api, ApiError } from "@/lib/api";
import { shortSha } from "@/lib/format";
import type { Repository, SnapshotRef } from "@/lib/types";
import { Badge, Modal } from "./ui";

const REASONS: { id: string; label: string }[] = [
  { id: "compiler_defect", label: "Compiler defect" },
  { id: "leaked_secret", label: "Leaked secret" },
  { id: "policy_violation", label: "Policy violation" },
  { id: "other", label: "Other" },
];

/** A snapshot's lifecycle state, from the repository registry. */
export function snapshotState(s: SnapshotRef): "retracted" | "quarantined" | null {
  return s.retracted ? "retracted" : s.quarantined ? "quarantined" : null;
}

export function SnapshotStateBadge({ snapshot }: { snapshot: SnapshotRef }) {
  const state = snapshotState(snapshot);
  if (state === "retracted") return <Badge tone="danger" title="Retracted: this snapshot can no longer be read">retracted</Badge>;
  if (state === "quarantined") return <Badge tone="candidate" title="Quarantined: a shadow compile disagreed with it; it is not its ref's current snapshot">quarantined</Badge>;
  return null;
}

function repoPath(repo: Repository, rest: string): string {
  return `repositories/${repo.repository_id}/${rest}`;
}

/** Shown in place of a snapshot that was retracted: why, and where its replacement is. */
export function RetractedNote({ error, snapshots, onOpen }: { error: ApiError; snapshots: SnapshotRef[]; onOpen: (hash: string) => void }) {
  const reason = typeof error.details?.reason === "string" ? error.details.reason : null;
  const replacement = typeof error.details?.replacement === "string" ? error.details.replacement : null;
  const target = replacement ? snapshots.find((s) => s.rkp_hash === replacement) : undefined;
  return (
    <section className="welcome" role="alert">
      <span className="eyebrow">RETRACTED</span>
      <h2>This snapshot was retracted and can no longer be read.</h2>
      <p>
        {reason ? `Reason: ${REASONS.find((r) => r.id === reason)?.label ?? reason}. ` : ""}
        {replacement ? "A replacement snapshot was named." : "No replacement was named."}
      </p>
      {target && (
        <button type="button" className="btn btn-primary" onClick={() => onOpen(target.rkp_hash)}>
          Open the replacement ({shortSha(target.commit_sha)} · {target.ref})
        </button>
      )}
    </section>
  );
}

/** Administrator actions on the selected snapshot: promote a quarantined one, or retract it. */
export function SnapshotActions({ repo, snapshot, snapshots, onChanged }: { repo: Repository; snapshot: SnapshotRef; snapshots: SnapshotRef[]; onChanged: (select: string | null) => void }) {
  const [retracting, setRetracting] = useState(false);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const state = snapshotState(snapshot);
  if (state === "retracted") return null;

  async function promote() {
    setBusy(true);
    setProblem(null);
    try {
      await api(repoPath(repo, `snapshots/${snapshot.rkp_hash}/promote`), { body: {} });
      onChanged(snapshot.rkp_hash);
    } catch (err) {
      setProblem(err instanceof ApiError ? err.message : "Could not promote the snapshot.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <span className="snapshot-actions">
      {state === "quarantined" && (
        <button type="button" className="btn btn-quiet btn-small" disabled={busy} onClick={() => void promote()} title="Release this snapshot from quarantine and make it its ref's current snapshot">
          <ShieldCheck size={13} aria-hidden="true" /> Promote
        </button>
      )}
      <button type="button" className="btn btn-quiet btn-small" disabled={busy} onClick={() => setRetracting(true)}>
        <Archive size={13} aria-hidden="true" /> Retract…
      </button>
      {problem && <span className="form-error" role="alert">{problem}</span>}
      {retracting && <RetractDialog repo={repo} snapshot={snapshot} snapshots={snapshots} onClose={() => setRetracting(false)} onDone={(next) => { setRetracting(false); onChanged(next); }} />}
    </span>
  );
}

function RetractDialog({ repo, snapshot, snapshots, onClose, onDone }: { repo: Repository; snapshot: SnapshotRef; snapshots: SnapshotRef[]; onClose: () => void; onDone: (replacement: string | null) => void }) {
  const [reason, setReason] = useState("compiler_defect");
  const [replacement, setReplacement] = useState("");
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const candidates = snapshots.filter((s) => s.rkp_hash !== snapshot.rkp_hash && !snapshotState(s));

  async function submit() {
    setBusy(true);
    setProblem(null);
    try {
      const out = await api<{ replacement: string | null }>(repoPath(repo, `snapshots/${snapshot.rkp_hash}/retract`), { body: { reason, replacement: replacement || null } });
      onDone(out.replacement);
    } catch (err) {
      setProblem(err instanceof ApiError ? err.message : "Could not retract the snapshot.");
      setBusy(false);
    }
  }

  return (
    <Modal title="Retract snapshot" onClose={onClose}>
      <p>
        Retracting <code>{shortSha(snapshot.commit_sha)}</code> on <strong>{snapshot.ref}</strong> makes it unreadable everywhere and deletes its content. Reads of it will point at the replacement. This cannot be undone.
      </p>
      <label className="field"><span>Reason</span>
        <select value={reason} onChange={(e) => setReason(e.target.value)} data-autofocus>
          {REASONS.map((r) => <option key={r.id} value={r.id}>{r.label}</option>)}
        </select>
      </label>
      <label className="field"><span>Replacement</span>
        <select value={replacement} onChange={(e) => setReplacement(e.target.value)}>
          <option value="">Newest on the same ref (automatic)</option>
          {candidates.map((s) => <option key={s.rkp_hash} value={s.rkp_hash}>{shortSha(s.commit_sha)} · {s.ref}</option>)}
        </select>
      </label>
      {problem && <p className="form-error" role="alert">{problem}</p>}
      <div className="form-actions">
        <button type="button" className="btn btn-quiet" onClick={onClose} disabled={busy}>Cancel</button>
        <button type="button" className="btn btn-danger" onClick={() => void submit()} disabled={busy}>{busy ? "Retracting…" : "Retract snapshot"}</button>
      </div>
    </Modal>
  );
}
