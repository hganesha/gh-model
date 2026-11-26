"use client";

// Coverage per plane (docs/solution-ir-contract.md 8.4). The map card and the plane view
// show the coverage domains that bear on the plane; the plane view of the experience,
// business and security planes adds entity counts per kind, the share an owner confirmed
// where a kind has a status, and the roles and permissions found by realm.

import { snapshotPath, useApi } from "@/lib/api";
import { kindLabel, plural } from "@/lib/format";
import { confirmedRatio, planeKindCounts, planeMeasures, securityRealms, STATUS_KINDS, STATUS_LIMIT, statusShare, statusKindsToFetch, type PlaneMeasure } from "@/lib/plane-coverage";
import { domainLabel, type PlaneMeta } from "@/lib/planes";
import type { EntitiesResponse, Summary } from "@/lib/types";
import { Coverage, ErrorNote, Loading } from "./ui";

/** Planes that get the review panel: the ones the solution IR added. */
export const REVIEW_PLANES = ["experience", "business", "security"];

export function measureLabel(m: PlaneMeasure): string {
  return m.platform ? `${m.platform}: ${domainLabel(m.domain).toLowerCase()}` : domainLabel(m.domain);
}

/** The coverage measures of a plane, as Coverage bars. Renders nothing when there are none. */
export function PlaneMeasures({ summary, plane, compact = false }: { summary: Summary; plane: PlaneMeta; compact?: boolean }) {
  const measures = planeMeasures(summary, plane);
  return (
    <>
      {measures.map((m) => (
        <Coverage key={m.key} compact={compact} label={measureLabel(m)} ratio={m.ratio} />
      ))}
    </>
  );
}

/** Roles and permissions counted from the summary, for the security plane's map card. */
export function securityFound(summary: Summary): string {
  const counts = summary.counts_by_kind ?? {};
  return `${plural(counts.Role ?? 0, "role")} and ${plural(counts.Permission ?? 0, "permission")} found`;
}

function StatusRow({ hash, plane, kind, total }: { hash: string; plane: string; kind: string; total: number }) {
  const params = new URLSearchParams({ plane, kind, limit: String(STATUS_LIMIT) });
  const { data, error, loading, reload } = useApi<EntitiesResponse>(snapshotPath(hash, `entities?${params}`));
  if (loading) return <Loading label={`Reading ${kindLabel(kind).toLowerCase()} statuses`} />;
  if (error) return <ErrorNote error={error} onRetry={reload} what={`${kindLabel(kind).toLowerCase()} statuses`} />;
  if (!data) return null;
  const share = statusShare(kind, total, data.entities);
  const ratio = confirmedRatio(share);
  const others = Object.entries(share.other).sort(([a], [b]) => a.localeCompare(b));
  return (
    <div className="plane-status">
      <Coverage compact label={`${kindLabel(kind)}: confirmed by an owner`} ratio={ratio} />
      {ratio ? (
        <p className="group-note">
          {plural(share.confirmed, "confirmed")}, {plural(share.hypothesis, "hypothesis", "hypotheses")}
          {others.map(([status, n]) => `, ${n.toLocaleString("en-US")} ${status}`).join("")}
          {share.missing > 0 ? `, ${share.missing.toLocaleString("en-US")} with no status` : ""}
          {data.truncated ? `; counted over the first ${share.counted.toLocaleString("en-US")} of ${total.toLocaleString("en-US")}` : ""}.
        </p>
      ) : (
        <p className="group-note">No status is recorded on these entities, so none can be counted as confirmed.</p>
      )}
    </div>
  );
}

function SecurityRealms({ hash }: { hash: string }) {
  const params = new URLSearchParams({ plane: "security", limit: String(STATUS_LIMIT) });
  const { data, error, loading, reload } = useApi<EntitiesResponse>(snapshotPath(hash, `entities?${params}`));
  if (loading) return <Loading label="Reading roles and permissions" />;
  if (error) return <ErrorNote error={error} onRetry={reload} what="roles and permissions" />;
  if (!data) return null;
  const realms = securityRealms(data.entities);
  if (realms.length === 0) return null;
  return (
    <table className="plane-table" aria-label="Roles and permissions by realm">
      {data.truncated && <caption>Counted over the first {data.entities.length.toLocaleString("en-US")} roles and permissions.</caption>}
      <thead>
        <tr>
          <th scope="col">Realm</th>
          <th scope="col">Roles</th>
          <th scope="col">Permissions</th>
        </tr>
      </thead>
      <tbody>
        {realms.map((r) => (
          <tr key={r.realm}>
            <td>
              <code>{r.realm}</code>
            </td>
            <td>{r.roles.toLocaleString("en-US")}</td>
            <td>{r.permissions.toLocaleString("en-US")}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/** Entity counts per kind, the share confirmed where a kind has a status, and the security findings. */
export function PlaneReview({ hash, summary, plane }: { hash: string; summary: Summary; plane: PlaneMeta }) {
  if (!REVIEW_PLANES.includes(plane.id)) return null;
  const counts = planeKindCounts(summary, plane);
  const withStatus = statusKindsToFetch(summary, plane);
  const roles = summary.counts_by_kind?.Role ?? 0;
  const permissions = summary.counts_by_kind?.Permission ?? 0;
  return (
    <section className="plane-review" aria-label={`${plane.title} coverage`}>
      <header>
        <h3>What this plane holds</h3>
        <p>Entities found per kind{plane.kinds.some((k) => STATUS_KINDS.includes(k)) ? ", and how many an owner confirmed where a kind is reviewed" : ""}. A kind with none found was looked for and not seen, which is not proof of absence.</p>
      </header>
      <table className="plane-table" aria-label={`${plane.title} entities per kind`}>
        <thead>
          <tr>
            <th scope="col">Kind</th>
            <th scope="col">Found</th>
          </tr>
        </thead>
        <tbody>
          {counts.map((k) => (
            <tr key={k.kind} className={k.count === 0 ? "is-zero" : ""}>
              <td>{kindLabel(k.kind)}</td>
              <td>{k.count === 0 ? "none found" : k.count.toLocaleString("en-US")}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {withStatus.map((kind) => (
        <StatusRow key={kind} hash={hash} plane={plane.id} kind={kind} total={counts.find((k) => k.kind === kind)?.count ?? 0} />
      ))}
      {plane.id === "security" && (
        <>
          <p className="group-note">{securityFound(summary)}.</p>
          {roles + permissions > 0 && <SecurityRealms hash={hash} />}
        </>
      )}
    </section>
  );
}
