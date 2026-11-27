# Changelog

## 0.1.0 (unreleased)

- Analyze Repository: runs the `rkp` engine and writes `.rkp/knowledge/` in the repository. Uncommitted changes are analysed by default (`rkp.analysis.includeUncommittedChanges`), with nothing written to `.git`.
- Platform packages carry a frozen engine, so no Python is needed; the engine settings and `PATH` still take precedence or serve as fallback.
- Open Explorer: the Explorer's map, plane, graph, quality and decision views in an editor tab, read from the bundle through `rkp serve --stdio`. Owner corrections are saved to `.rkp/overrides.json`.
- Knowledge view: components, endpoints, tables, event topics, deployment units, AI components, jobs, programs, architecture patterns and antipatterns, with their relations, conflicts and unknowns.
- Go to evidence: selects the exact lines each item rests on, and warns when the file has changed since the analysis.
- Conflicts appear in the Problems panel.
- Status bar: whether the knowledge describes the current source, toolchain and owner overrides; checked again after edits and when the window regains focus.
- Reads bundles in format `rkp-bundle/2`, which leaves default-valued fields out of each row (about 30% smaller), and still reads format 1. The bundled engine writes format 2.
- Commits the architecture tier by default: function-level detail stays in the local cache (StackGraph: 6 MB committed instead of 15 MB). On a fresh clone the status bar reads "Knowledge current (architecture)" until you analyse. `"commit": "full"` in `.rkp/config.json` commits everything.
- In an untrusted workspace the engine never runs; existing knowledge can still be browsed.
- RKP: Use this document for...: a multi-select of requirements, business rules, data schema and code lists for a document, saved to `.rkp/documents.json` with `rkp documents set`, then an offer to analyse again. A change to `.rkp/documents.json` makes the status bar check again.
