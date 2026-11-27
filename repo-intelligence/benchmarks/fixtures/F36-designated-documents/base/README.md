# Order desk

A small order desk: the tables in `db/schema.sql` and the documents under `docs/` that
describe them. `.rkp/documents.json` says which documents the engine reads, and for what:

- `docs/requirements.md` for requirements;
- `docs/data-dictionary.docx` for the schema it describes;
- `docs/order-codes.xlsx` (by a glob) for code lists;
- `docs/pricing-rules.md` for business rules, lifted only when a model is allowed;
- `docs/scanned-policy.pdf` for rules, which cannot be read until a text export is committed.

`docs/meeting-notes.md` and `docs/glossary.md` are not designated, so nothing is read from them.
