"use client";

import { useMemo, useState } from "react";
import { snapshotPath, useApi } from "@/lib/api";
import { plural } from "@/lib/format";
import type { DecisionsResponse } from "@/lib/types";
import { EntityLink, useEntityNames } from "./quality-view";
import { Badge, ErrorNote, Loading } from "./ui";
import { specFunction, useOverrides, type DecisionOverride } from "@/lib/overrides";
import type { Decision, EntityRef } from "@/lib/types";

type OverridesState = ReturnType<typeof useOverrides>;

function OverrideEditor({ decision, labels, overrides, onDone }: { decision: Decision; labels: string[]; overrides: OverridesState; onDone: () => void }) {
  const spec = specFunction(decision.spec);
  const existing = overrides.data?.decisions.find((o) => o.spec === spec && o.scope_entity === decision.scope_entity);
  const [label, setLabel] = useState(existing?.label ?? decision.selected_label ?? "");
  const [reason, setReason] = useState(existing?.reason ?? "");
  const listId = `labels-${decision.decision_id}`;
  async function save() {
    const row: DecisionOverride = { spec, scope_entity: decision.scope_entity!, label: label.trim(), reason: reason.trim() };
    const ok = await overrides.save((cur) => ({ ...cur, decisions: [...cur.decisions.filter((o) => !(o.spec === spec && o.scope_entity === row.scope_entity)), row] }));
    if (ok) onDone();
  }
  return (
    <div className="override-row">
      <label className="field"><span>Owner label</span><input list={listId} value={label} onChange={(e) => setLabel(e.target.value)} data-autofocus /></label>
      <datalist id={listId}>{labels.map((l) => <option key={l} value={l} />)}</datalist>
      <label className="field"><span>Reason</span><input value={reason} maxLength={500} onChange={(e) => setReason(e.target.value)} /></label>
      <button type="button" className="btn btn-primary btn-small" disabled={!label.trim() || overrides.saving} onClick={() => void save()}>Save override</button>
      <button type="button" className="btn btn-quiet btn-small" onClick={onDone}>Cancel</button>
    </div>
  );
}

function OverridesPanel({ overrides, names, onOpen }: { overrides: OverridesState; names: Record<string, EntityRef>; onOpen: (id: string) => void }) {
  const { data, error, loading, reload } = overrides;
  if (loading) return <Loading label="Loading owner overrides" />;
  if (error) return <ErrorNote error={error} onRetry={reload} what="owner overrides" />;
  if (!data) return null;
  const total = data.decisions.length + data.preferences.length;
  return (
    <section className="panel">
      <h3>Owner overrides <span className="section-count">version {data.version}</span></h3>
      <p className="group-note">An owner override outranks the compiler. The compiler still decides, and a disagreement is kept as a <code>label_disagreement</code> conflict with the owner preferred. Changes apply from the next compile: recompile to see them.</p>
      {total === 0 && <p className="group-note">No overrides are set for this repository.</p>}
      <ul className="lineage-list">
        {data.decisions.map((o) => (
          <li key={`d:${o.spec}:${o.scope_entity}`}>
            <code>{o.spec}</code> <EntityLink id={o.scope_entity} names={names} onOpen={onOpen} /> → <Badge tone="verified">{o.label}</Badge>
            {o.reason && <span className="muted">{o.reason}</span>}
            <button type="button" className="link-btn" disabled={overrides.saving} onClick={() => void overrides.save((cur) => ({ ...cur, decisions: cur.decisions.filter((x) => x !== o) }))}>Remove</button>
          </li>
        ))}
        {data.preferences.map((o) => (
          <li key={`p:${o.predicate}:${o.subject}`}>
            <span>Prefer</span> <EntityLink id={o.object} names={names} onOpen={onOpen} /> <span className="muted">as</span> <code>{o.predicate}</code> <span className="muted">of</span> <EntityLink id={o.subject} names={names} onOpen={onOpen} />
            {o.reason && <span className="muted">{o.reason}</span>}
            <button type="button" className="link-btn" disabled={overrides.saving} onClick={() => void overrides.save((cur) => ({ ...cur, preferences: cur.preferences.filter((x) => x !== o) }))}>Remove</button>
          </li>
        ))}
      </ul>
      {overrides.problem && <p className="form-error" role="alert">{overrides.problem}</p>}
    </section>
  );
}

function Scores({ scores, selected }: { scores: Record<string, number>; selected: string | null }) {
  const entries = Object.entries(scores).filter(([, v]) => typeof v === "number");
  if (!entries.length) return <span className="muted">none</span>;
  const max = Math.max(...entries.map(([, v]) => v), 0) || 1;
  return (
    <ul className="scores">
      {entries
        .sort((a, b) => b[1] - a[1])
        .map(([label, value]) => (
          <li key={label} className={label === selected ? "is-selected" : ""}>
            <span className="score-label">{label}</span>
            <span className="score-bar" aria-hidden="true">
              <i style={{ width: `${Math.max(0, Math.min(100, (value / max) * 100))}%` }} />
            </span>
            <span className="score-value">{value.toFixed(2)}</span>
          </li>
        ))}
    </ul>
  );
}

export function DecisionsView({ hash, repositoryId, onOpenEntity }: { hash: string; repositoryId: string | null; onOpenEntity: (id: string) => void }) {
  const { data, error, loading, reload } = useApi<DecisionsResponse>(snapshotPath(hash, "decisions"));
  const overrides = useOverrides(repositoryId);
  const [editing, setEditing] = useState<string | null>(null);
  const scopeIds = useMemo(() => [
    ...(data?.decisions ?? []).map((d) => d.scope_entity),
    ...(overrides.data?.decisions ?? []).map((o) => o.scope_entity),
    ...(overrides.data?.preferences ?? []).flatMap((o) => [o.subject, o.object]),
  ].filter((id): id is string => !!id), [data, overrides.data]);
  // Labels seen for each decision function, offered when an owner overrides one.
  const labelsBySpec = useMemo(() => {
    const m = new Map<string, Set<string>>();
    for (const d of data?.decisions ?? []) {
      const set = m.get(specFunction(d.spec)) ?? new Set<string>();
      if (d.selected_label) set.add(d.selected_label);
      for (const l of Object.keys(d.option_scores ?? {})) set.add(l);
      m.set(specFunction(d.spec), set);
    }
    return m;
  }, [data]);
  const { names } = useEntityNames(hash, scopeIds);
  const decisions = data?.decisions ?? [];
  const abstained = decisions.filter((d) => d.abstained).length;

  return (
    <section className="decisions-view" aria-label="Decisions">
      <header className="lead">
        <span className="eyebrow">DECISION LEDGER</span>
        <h2>Decisions</h2>
        <p>Every classification the compiler made by rule or by model while building this snapshot, including the ones where it declined to choose.</p>
      </header>
      {loading && <Loading label="Loading decisions" />}
      {error && <ErrorNote error={error} onRetry={reload} what="decisions" />}
      {data && decisions.length === 0 && <p className="state">No decisions were recorded for this snapshot.</p>}
      {data && decisions.length > 0 && (
        <section className="panel">
          <h3>
            {plural(decisions.length, "decision")} <span className="section-count">{abstained} abstained</span>
          </h3>
          <div className="table-scroll">
            <table className="data-table decisions-table">
              <thead>
                <tr>
                  <th scope="col">Spec</th>
                  <th scope="col">Scope entity</th>
                  <th scope="col">Rule outcome</th>
                  <th scope="col">Label</th>
                  <th scope="col">Method</th>
                  <th scope="col">Model</th>
                  <th scope="col">Option scores</th>
                  <th scope="col"><span className="sr-only">Owner override</span></th>
                </tr>
              </thead>
              <tbody>
                {decisions.map((d) => {
                  const owner = overrides.data?.decisions.find((o) => o.spec === specFunction(d.spec) && o.scope_entity === d.scope_entity);
                  return [
                  <tr key={d.decision_id} className={d.abstained ? "is-abstained" : ""}>
                    <td>
                      <code>{d.spec}</code>
                    </td>
                    <td>{d.scope_entity ? <EntityLink id={d.scope_entity} names={names} onOpen={onOpenEntity} /> : <span className="muted">none</span>}</td>
                    <td>{d.rule_outcome ?? <span className="muted">unknown</span>}</td>
                    <td>
                      {d.abstained ? (
                        <span className="abstained">abstained: {d.abstain_reason ?? "no reason given"}</span>
                      ) : d.selected_label !== null && d.selected_label !== undefined ? (
                        <Badge tone="supported">{d.selected_label}</Badge>
                      ) : (
                        <span className="muted">unknown</span>
                      )}
                    </td>
                    <td>
                      <Badge tone={d.decision_method === "decision_model" ? "inferred" : "neutral"}>{d.decision_method}</Badge>
                    </td>
                    <td>{d.model_ref ? <code>{d.model_ref}</code> : <span className="muted">none</span>}</td>
                    <td>{d.option_scores ? <Scores scores={d.option_scores} selected={d.selected_label} /> : <span className="muted">none</span>}</td>
                    <td>
                      {owner && <Badge tone="verified" title={owner.reason || "Set by the repository owner"}>owner: {owner.label}</Badge>}
                      {d.scope_entity && overrides.data && <button type="button" className="link-btn" onClick={() => setEditing(editing === d.decision_id ? null : d.decision_id)}>{owner ? "Change" : "Override"}</button>}
                    </td>
                  </tr>,
                  editing === d.decision_id && (
                    <tr key={`${d.decision_id}:edit`}><td colSpan={8}>
                      <OverrideEditor decision={d} labels={[...(labelsBySpec.get(specFunction(d.spec)) ?? [])].sort()} overrides={overrides} onDone={() => setEditing(null)} />
                      {overrides.problem && <p className="form-error" role="alert">{overrides.problem}</p>}
                    </td></tr>
                  ),
                  ];
                })}
              </tbody>
            </table>
          </div>
        </section>
      )}
      {repositoryId && <OverridesPanel overrides={overrides} names={names} onOpen={onOpenEntity} />}
    </section>
  );
}
