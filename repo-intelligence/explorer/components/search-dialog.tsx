"use client";

import { Search } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { snapshotPath, useApi } from "@/lib/api";
import { entityContext, kindLabel } from "@/lib/format";
import { planeOfKind } from "@/lib/planes";
import type { EntitiesResponse, Entity } from "@/lib/types";
import { ErrorNote, Loading, Modal } from "./ui";

const LIMIT = 60;

export function SearchDialog({ hash, onClose, onOpenEntity }: { hash: string | null; onClose: () => void; onOpenEntity: (id: string) => void }) {
  const [text, setText] = useState("");
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const list = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const t = setTimeout(() => setQuery(text.trim()), 220);
    return () => clearTimeout(t);
  }, [text]);

  const { data, error, loading, reload } = useApi<EntitiesResponse>(hash && query ? snapshotPath(hash, `entities?${new URLSearchParams({ q: query, limit: String(LIMIT) })}`) : null);

  // Group by kind, keeping one flat order for arrow-key navigation.
  const { groups, flat } = useMemo(() => {
    const byKind = new Map<string, Entity[]>();
    for (const e of data?.entities ?? []) {
      if (!byKind.has(e.kind)) byKind.set(e.kind, []);
      byKind.get(e.kind)!.push(e);
    }
    const ordered = [...byKind.entries()].sort((a, b) => Number(a[0] === "Artifact") - Number(b[0] === "Artifact") || a[0].localeCompare(b[0]));
    return { groups: ordered, flat: ordered.flatMap(([, items]) => items) };
  }, [data]);

  useEffect(() => setActive(0), [data]);
  useEffect(() => {
    list.current?.querySelector('[aria-selected="true"]')?.scrollIntoView({ block: "nearest" });
  }, [active]);

  function choose(entity: Entity | undefined) {
    if (!entity) return;
    onClose();
    onOpenEntity(entity.id);
  }

  function onKeyDown(e: React.KeyboardEvent) {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setActive((i) => Math.min(flat.length - 1, i + 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActive((i) => Math.max(0, i - 1));
    } else if (e.key === "Enter") {
      e.preventDefault();
      choose(flat[active]);
    }
  }

  let index = -1;
  return (
    <Modal title="Search entities" onClose={onClose} className="search-modal" hideTitle>
      <div className="search-input">
        <Search size={16} aria-hidden="true" />
        <input
          data-autofocus
          type="search"
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={onKeyDown}
          placeholder={hash ? "Search entities across every plane" : "Select a snapshot to search"}
          disabled={!hash}
          aria-label="Search entities"
          aria-controls="search-results"
          autoComplete="off"
          spellCheck={false}
        />
        <kbd>Esc</kbd>
      </div>
      <div className="search-results" id="search-results" ref={list} role="listbox" aria-label="Search results">
        {!hash && <p className="state">There is no snapshot to search yet.</p>}
        {hash && !query && <p className="state">Type a name, path or route. Results are grouped by kind.</p>}
        {loading && <Loading label="Searching" />}
        {error && <ErrorNote error={error} onRetry={reload} what="search results" />}
        {data && flat.length === 0 && <p className="state">No entity in this snapshot matches that text.</p>}
        {groups.map(([kind, items]) => (
          <div className="search-group" key={kind} role="group" aria-label={kindLabel(kind)}>
            <h3>
              <i className={`dot plane-${planeOfKind(kind)}`} aria-hidden="true" />
              {kindLabel(kind)} <span>{items.length}</span>
            </h3>
            {items.map((entity) => {
              index += 1;
              const i = index;
              const context = entityContext(entity);
              return (
                <button type="button" key={entity.id} role="option" aria-selected={i === active} className={`search-hit ${i === active ? "is-active" : ""}`} onMouseEnter={() => setActive(i)} onClick={() => choose(entity)}>
                  <span className="entity-name">{entity.name}</span>
                  {context && <span className="entity-context">{context}</span>}
                </button>
              );
            })}
          </div>
        ))}
        {data?.truncated && <p className="group-note group-truncated">Showing first {flat.length} matches. Refine the search to narrow them down.</p>}
      </div>
    </Modal>
  );
}
