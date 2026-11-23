"use client";

import { useMemo } from "react";
import { useApi } from "@/lib/api";
import { displayValue, kindLabel, measured, percent, percentLabel, plural, shortId } from "@/lib/format";
import { domainLabel } from "@/lib/planes";
import type { CoverageRow, EntityRef, QueryResponse, Ratio, Summary } from "@/lib/types";
import { Badge, Bar, ErrorNote } from "./ui";

/**
 * Resolve entity ids to names with one depth-0 graph query. Until it answers
 * (or if it fails) callers show shortened ids instead.
 */
export function useEntityNames(hash: string, ids: string[]): { names: Record<string, EntityRef>; loading: boolean; failed: boolean } {
  const unique = useMemo(() => [...new Set(ids)].sort().slice(0, 1000), [ids]);
  const { data, loading, error } = useApi<QueryResponse>(
    unique.length ? "knowledge/query" : null,
    unique.length ? { rkp_hash: hash, seed_entities: unique, max_depth: 0, max_nodes: unique.length } : undefined,
  );
  const names = useMemo(() => {
    const map: Record<string, EntityRef> = {};
    for (const e of data?.entities ?? []) map[e.id] = { id: e.id, kind: e.kind, name: e.name };
    return map;
  }, [data]);
  return { names, loading, failed: !!error };
}

export function EntityLink({ id, names, onOpen }: { id: string; names: Record<string, EntityRef>; onOpen: (id: string) => void }) {
  const ref = names[id];
  return (
    <button type="button" className="entity-link" onClick={() => onOpen(id)} title={ref ? kindLabel(ref.kind) : "Name not resolved yet"}>
      {ref ? ref.name : <code>{shortId(id)}</code>}
    </button>
  );
}

function ratioCell(r: Ratio) {
  if (!measured(r)) return <span className="coverage-unmeasured">not measured</span>;
  return (
    <>
      <b>{r.numerator.toLocaleString("en-US")}</b> of {r.denominator.toLocaleString("en-US")}
    </>
  );
}

/** Small numeric facts from a coverage row's detail, e.g. "python 40/40 · shell 0/5". */
function detailFacts(detail: Record<string, unknown>): string[] {
  const facts: string[] = [];
  for (const [key, value] of Object.entries(detail ?? {})) {
    if (typeof value === "number") facts.push(`${key} ${value.toLocaleString("en-US")}`);
    else if (key === "by_language" && value && typeof value === "object") {
      for (const [lang, v] of Object.entries(value as Record<string, { files?: number; parsed?: number }>)) {
        if (v && typeof v.files === "number") facts.push(`${lang} ${v.parsed ?? 0}/${v.files}`);
      }
    }
  }
  return facts;
}

const IMPORT_PARTS = ["internal", "workspace", "external", "stdlib", "unresolved"] as const;

function ImportBreakdown({ rows }: { rows: CoverageRow[] }) {
  const totals = Object.fromEntries(IMPORT_PARTS.map((p) => [p, 0])) as Record<(typeof IMPORT_PARTS)[number], number>;
  for (const row of rows) for (const part of IMPORT_PARTS) totals[part] += typeof row.detail?.[part] === "number" ? (row.detail[part] as number) : 0;
  const sum = IMPORT_PARTS.reduce((n, p) => n + totals[p], 0);
  const scoped = rows
    .map((r) => ({ scope: r.scope, imports: Array.isArray(r.detail?.unresolved_imports) ? (r.detail.unresolved_imports as unknown[]).map(displayValue) : [], count: typeof r.detail?.unresolved === "number" ? (r.detail.unresolved as number) : 0 }))
    .filter((r) => r.count > 0 || r.imports.length > 0);

  return (
    <section className="panel">
      <h3>Import resolution breakdown</h3>
      {sum === 0 ? (
        <p className="group-note">No imports were recorded in this snapshot.</p>
      ) : (
        <>
          <div className="stack-bar" role="img" aria-label={IMPORT_PARTS.map((p) => `${totals[p]} ${p}`).join(", ")}>
            {IMPORT_PARTS.map((p) => (totals[p] > 0 ? <span key={p} className={`stack-${p}`} style={{ flexGrow: totals[p] }} /> : null))}
          </div>
          <ul className="stack-legend">
            {IMPORT_PARTS.map((p) => (
              <li key={p}>
                <i className={`stack-${p}`} aria-hidden="true" />
                {p} <b>{totals[p].toLocaleString("en-US")}</b>
              </li>
            ))}
          </ul>
        </>
      )}
      {scoped.map((s) => (
        <details key={s.scope} className="expander">
          <summary>
            <span>
              Unresolved imports in <code>{s.scope}</code>
            </span>
            <Badge tone="candidate">{s.count.toLocaleString("en-US")}</Badge>
          </summary>
          {s.imports.length === 0 ? (
            <p className="group-note">The server did not list the individual imports for this scope.</p>
          ) : (
            <>
              {s.imports.length < s.count && <p className="group-note">The server listed {s.imports.length} of {s.count}.</p>}
              <ul className="mono-list">
                {s.imports.map((imp, i) => (
                  <li key={`${i}:${imp}`}>{imp}</li>
                ))}
              </ul>
            </>
          )}
        </details>
      ))}
    </section>
  );
}

export function QualityView({ hash, summary, onOpenEntity }: { hash: string; summary: Summary; onOpenEntity: (id: string) => void }) {
  const totals = Object.entries(summary.coverage_totals ?? {});
  const ranked = totals.filter(([, r]) => measured(r)).sort((a, b) => percent(a[1]) - percent(b[1]) || b[1].denominator - a[1].denominator);
  const weakest = ranked[0];
  const unmeasured = totals.filter(([, r]) => !measured(r)).map(([d]) => d);

  const domains = useMemo(() => {
    const order = [...new Set([...Object.keys(summary.coverage_totals ?? {}), ...summary.coverage.map((c) => c.domain)])];
    return order.map((domain) => ({ domain, total: summary.coverage_totals?.[domain] ?? null, rows: summary.coverage.filter((c) => c.domain === domain) }));
  }, [summary]);

  const unknownGroups = useMemo(() => {
    const groups = new Map<string, { reason: string; subjects: string[]; predicates: Set<string> }>();
    for (const u of summary.unknowns) {
      const reason = u.reason || "no reason given";
      if (!groups.has(reason)) groups.set(reason, { reason, subjects: [], predicates: new Set() });
      const g = groups.get(reason)!;
      g.subjects.push(u.subject);
      g.predicates.add(u.predicate);
    }
    return [...groups.values()].sort((a, b) => b.subjects.length - a.subjects.length);
  }, [summary]);

  const subjectIds = useMemo(() => [...summary.unknowns.map((u) => u.subject), ...summary.conflicts.flatMap((c) => c.subject_ids ?? [])], [summary]);
  const { names, failed } = useEntityNames(hash, subjectIds);

  return (
    <section className="quality-view" aria-label="Quality">
      <header className="lead">
        <span className="eyebrow">07 · QUALITY</span>
        <h2>
          {weakest
            ? `Weakest coverage: ${domainLabel(weakest[0]).toLowerCase()} — ${weakest[1].numerator.toLocaleString("en-US")} of ${weakest[1].denominator.toLocaleString("en-US")} (${percentLabel(weakest[1])}).`
            : "No coverage domain was measured for this snapshot."}
        </h2>
        <p>
          {plural(summary.unknowns.length, "unknown")} and {plural(summary.conflicts.length, "conflict")} recorded.
          {unmeasured.length > 0 && ` Not measured (nothing to measure against): ${unmeasured.map((d) => domainLabel(d).toLowerCase()).join(", ")}.`}
        </p>
      </header>

      <section className="panel">
        <h3>Coverage</h3>
        <div className="table-scroll">
          <table className="data-table coverage-table">
            <thead>
              <tr>
                <th scope="col">Domain</th>
                <th scope="col">Scope</th>
                <th scope="col" className="num">
                  Resolved
                </th>
                <th scope="col" className="num">
                  %
                </th>
                <th scope="col">
                  <span className="sr-only">Bar</span>
                </th>
              </tr>
            </thead>
            {domains.map(({ domain, total, rows }) => (
              <tbody key={domain}>
                <tr className="domain-row">
                  <th scope="row">
                    {domainLabel(domain)}
                    <code>{domain}</code>
                  </th>
                  <td>all scopes</td>
                  <td className="num">{total ? ratioCell(total) : "unknown"}</td>
                  <td className="num">{measured(total) ? percentLabel(total) : "—"}</td>
                  <td className="bar-cell">
                    <Bar ratio={measured(total) ? total : null} />
                  </td>
                </tr>
                {rows.map((row) => {
                  const facts = detailFacts(row.detail);
                  return (
                    <tr key={`${domain}:${row.scope}`} className="scope-row">
                      <td />
                      <td>
                        <code>{row.scope}</code>
                        {facts.length > 0 && <span className="facts">{facts.join(" · ")}</span>}
                      </td>
                      <td className="num">{ratioCell(row)}</td>
                      <td className="num">{measured(row) ? percentLabel(row) : "—"}</td>
                      <td className="bar-cell">
                        <Bar ratio={measured(row) ? row : null} />
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            ))}
          </table>
        </div>
      </section>

      <ImportBreakdown rows={summary.coverage.filter((c) => c.domain === "import_resolution")} />

      <section className="panel">
        <h3>
          Unknowns <span className="section-count">{summary.unknowns.length}</span>
        </h3>
        <p className="group-note">An unknown is a relationship the compiler expected but could not resolve. It is not evidence that the relationship does not exist.</p>
        {failed && <ErrorNote error={{ message: "Entity names could not be resolved; ids are shown instead." }} />}
        {unknownGroups.length === 0 && <p className="group-note">No unknowns were recorded in this snapshot.</p>}
        {unknownGroups.map((g) => (
          <details key={g.reason} className="expander">
            <summary>
              <span>{g.reason}</span>
              <span className="expander-meta">
                <code>{[...g.predicates].join(", ")}</code>
                <Badge tone="unknown">{plural(g.subjects.length, "unknown")}</Badge>
              </span>
            </summary>
            <ul className="link-list">
              {g.subjects.map((id, i) => (
                <li key={`${id}:${i}`}>
                  <EntityLink id={id} names={names} onOpen={onOpenEntity} />
                </li>
              ))}
            </ul>
          </details>
        ))}
      </section>

      <section className="panel">
        <h3>
          Conflicts <span className="section-count">{summary.conflicts.length}</span>
        </h3>
        {summary.conflicts.length === 0 ? (
          <p className="group-note">No conflicts were recorded in this snapshot.</p>
        ) : (
          <ul className="conflict-list">
            {summary.conflicts.map((c) => (
              <li key={c.id}>
                <Badge tone="candidate">{c.conflict_type}</Badge>
                <div>
                  <p>{displayValue(c.basis)}</p>
                  <ul className="link-list">
                    {(c.subject_ids ?? []).map((id) => (
                      <li key={id}>
                        <EntityLink id={id} names={names} onOpen={onOpenEntity} />
                      </li>
                    ))}
                  </ul>
                </div>
              </li>
            ))}
          </ul>
        )}
      </section>
    </section>
  );
}
