"use client";

import type { Profile, ProfileField, ProfileValue } from "@/lib/types";

/** Group order; a group the server adds later is shown after these. */
const GROUP_ORDER = ["identity", "stack", "runtime", "upkeep"];

function ValueRow({ field, value, onOpenEntity, onOpenPlane }: { field: ProfileField; value: ProfileValue; onOpenEntity: (id: string) => void; onOpenPlane: (plane: string) => void }) {
  const body = (
    <>
      <b>{value.text}</b>
      {value.detail && <span>{value.detail}</span>}
    </>
  );
  // One entity opens in the drawer; several open the plane they belong to.
  const single = value.entity_ids.length === 1 && value.entities === 1 ? value.entity_ids[0] : null;
  const open = single ? () => onOpenEntity(single) : field.plane ? () => onOpenPlane(field.plane as string) : null;
  if (!open) return <span className="profile-value">{body}</span>;
  return (
    <button type="button" className="profile-value" onClick={open} aria-label={`${field.label}: ${value.text}${value.detail ? `, ${value.detail}` : ""}. Open ${single ? "this entity" : `the ${field.plane} plane`}`}>
      {body}
    </button>
  );
}

function Field({ field, onOpenEntity, onOpenPlane }: { field: ProfileField; onOpenEntity: (id: string) => void; onOpenPlane: (plane: string) => void }) {
  return (
    <div className={`profile-field ${field.status === "inferred" ? "is-inferred" : ""}`}>
      <dt>
        {field.label}
        {field.status === "inferred" && <i title="Chosen by a rule or a heuristic, not stated by the repository">inferred</i>}
      </dt>
      <dd>
        {field.values.map((value) => (
          <ValueRow key={`${value.text}:${value.entity_ids[0] ?? ""}`} field={field} value={value} onOpenEntity={onOpenEntity} onOpenPlane={onOpenPlane} />
        ))}
        {field.more > 0 && <span className="profile-more">and {field.more.toLocaleString("en-US")} more</span>}
      </dd>
    </div>
  );
}

/** What this application is, as labelled fields rolled up from the snapshot's facts. */
export function ProfileBand({ profile, description, onOpenEntity, onOpenPlane }: { profile: Profile; description?: string | null; onOpenEntity: (id: string) => void; onOpenPlane: (plane: string) => void }) {
  const groups = [...GROUP_ORDER.filter((g) => g in profile.groups), ...Object.keys(profile.groups).filter((g) => !GROUP_ORDER.includes(g))];
  const anything = profile.fields.some((f) => f.status !== "not_found");
  return (
    <section className="profile-band" aria-label="Repository profile">
      <header>
        <span className="eyebrow">Profile</span>
        {description && <p className="profile-description">{description}</p>}
      </header>
      {!anything ? (
        <p className="profile-empty">Nothing the compiler found says what this repository is. That is not proof of absence — it may not recognise how this repository is built.</p>
      ) : (
        <div className="profile-groups">
          {groups.map((group) => {
            const fields = profile.fields.filter((f) => f.group === group);
            const found = fields.filter((f) => f.status !== "not_found");
            const missing = fields.filter((f) => f.status === "not_found");
            if (fields.length === 0) return null;
            return (
              <section key={group} className="profile-group" aria-label={profile.groups[group]}>
                <h3>{profile.groups[group]}</h3>
                <dl>
                  {found.map((field) => (
                    <Field key={field.id} field={field} onOpenEntity={onOpenEntity} onOpenPlane={onOpenPlane} />
                  ))}
                </dl>
                {missing.length > 0 && <p className="profile-missing">None found: {missing.map((f) => f.label).join(", ")}</p>}
              </section>
            );
          })}
        </div>
      )}
      {anything && (
        <p className="profile-foot">
          Rolled up from this snapshot&apos;s facts. “None found” means the compiler found nothing, not that there is none.
          {profile.partial ? " This snapshot is large, so the profile reads only part of it." : ""}
        </p>
      )}
    </section>
  );
}
