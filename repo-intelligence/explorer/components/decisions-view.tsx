"use client";

import { useMemo } from "react";
import { snapshotPath, useApi } from "@/lib/api";
import { plural } from "@/lib/format";
import type { DecisionsResponse } from "@/lib/types";
import { EntityLink, useEntityNames } from "./quality-view";
import { Badge, ErrorNote, Loading } from "./ui";

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

export function DecisionsView({ hash, onOpenEntity }: { hash: string; onOpenEntity: (id: string) => void }) {
  const { data, error, loading, reload } = useApi<DecisionsResponse>(snapshotPath(hash, "decisions"));
  const scopeIds = useMemo(() => (data?.decisions ?? []).map((d) => d.scope_entity).filter((id): id is string => !!id), [data]);
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
                </tr>
              </thead>
              <tbody>
                {decisions.map((d) => (
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
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}
    </section>
  );
}
