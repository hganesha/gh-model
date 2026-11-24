"use client";

import { useCallback, useState } from "react";
import { api, ApiError, useApi } from "./api";

// Owner overrides (spec §8.6): the repository owner outranks the compiler. They are
// an input to the next compile, so a change shows up after recompiling.

export interface DecisionOverride {
  spec: string;
  scope_entity: string;
  label: string;
  reason: string;
}

export interface PreferenceOverride {
  predicate: string;
  subject: string;
  object: string;
  reason: string;
}

export interface OverrideSet {
  repository_id: string;
  version: number;
  override_set_hash: string;
  decisions: DecisionOverride[];
  preferences: PreferenceOverride[];
  history: { version: number; author: string; set_hash: string }[];
}

/** "dm.component-kind@1.1.0" -> "dm.component-kind": overrides name the function, not its version. */
export function specFunction(spec: string): string {
  return spec.split("@")[0];
}

export function useOverrides(repositoryId: string | null) {
  const state = useApi<OverrideSet>(repositoryId ? `repositories/${repositoryId}/overrides` : null);
  const [saving, setSaving] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const { data, reload } = state;

  const save = useCallback(
    async (change: (current: Pick<OverrideSet, "decisions" | "preferences">) => Pick<OverrideSet, "decisions" | "preferences">) => {
      if (!repositoryId || !data) return false;
      setSaving(true);
      setProblem(null);
      try {
        const next = change({ decisions: data.decisions, preferences: data.preferences });
        await api(`repositories/${repositoryId}/overrides`, { method: "PUT", body: { ...next, expected_version: data.version } });
        reload();
        return true;
      } catch (err) {
        setProblem(err instanceof ApiError ? (err.status === 409 ? "Someone else changed the overrides; reload and try again." : err.message) : "The overrides could not be saved.");
        return false;
      } finally {
        setSaving(false);
      }
    },
    [repositoryId, data, reload],
  );

  return { ...state, save, saving, problem };
}
