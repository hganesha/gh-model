"use client";

import { useState } from "react";
import { api, ApiError } from "@/lib/api";
import { shortSha } from "@/lib/format";
import type { Repository } from "@/lib/types";

interface RefView {
  ref: string;
  kind: string;
  state: "current" | "stale" | string;
  head_sha: string | null;
  commit_sha: string | null;
  closed: boolean;
}

/**
 * Open a tracked branch, tag or pull request by name (GET /rkp?ref=). Asking for a
 * lazily tracked pull request starts its compile, so this is also how one is requested.
 */
export function RefOpener({ repo, onOpen, onPending }: { repo: Repository; onOpen: (hash: string) => void; onPending: () => void }) {
  const [ref, setRef] = useState("");
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<{ ok: boolean; text: string } | null>(null);

  async function open(e: React.FormEvent) {
    e.preventDefault();
    const name = ref.trim();
    if (!name) return;
    setBusy(true);
    setNote(null);
    try {
      const out = await api<{ rkp_hash?: string; status?: string; ref_view: RefView }>(`repositories/${repo.repository_id}/rkp?ref=${encodeURIComponent(name)}`);
      const v = out.ref_view;
      if (out.rkp_hash) {
        onOpen(out.rkp_hash);
        setNote({ ok: true, text: v.state === "stale" ? `${v.ref} has moved to ${shortSha(v.head_sha)}; showing the last published snapshot (${shortSha(v.commit_sha)}) while it recompiles.` : `${v.ref} is current at ${shortSha(v.commit_sha)}.` });
      } else {
        setNote({ ok: true, text: `${v.ref} has no snapshot yet; its compile has been requested.` });
        onPending();
      }
    } catch (err) {
      setNote({ ok: false, text: err instanceof ApiError && err.status === 404 ? `${name} is not a tracked ref of this repository. Add it under Settings → Repositories.` : err instanceof Error ? err.message : "Could not open the ref." });
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="ref-opener" onSubmit={(e) => void open(e)}>
      <label className="sr-only" htmlFor={`ref-${repo.repository_id}`}>Open a tracked ref</label>
      <input id={`ref-${repo.repository_id}`} placeholder="Open a ref (branch, tag, refs/pull/1/head)" value={ref} onChange={(e) => setRef(e.target.value)} maxLength={200} />
      <button type="submit" className="btn btn-side" disabled={busy || !ref.trim()}>{busy ? "Opening…" : "Open"}</button>
      {note && <p className={note.ok ? "side-note" : "side-note form-error"} role={note.ok ? "status" : "alert"}>{note.text}</p>}
    </form>
  );
}
