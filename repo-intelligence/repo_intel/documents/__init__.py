"""Designated documents (docs/solution-ir-contract.md, section 9.1).

An owner marks in `.rkp/documents.json` which documents the engine reads and for which
features: `requirements`, `rules`, `schema`, `code_lists`. Nothing undesignated is read
for these features.

- `format`: the `rkp-documents/1` file: load with problems, glob matching, sorted writing.
- `readers`: Markdown, text, CSV, HTML, DOCX and XLSX cut into blocks.
- `features`: requirements, schemas, code lists and rule candidates from blocks.
- `steps`: the pure build steps; `stage`: how the compile pipeline runs them.
- `rules`: spec `dm.document-rules`, behind `CompileSettings.business_rules`.
- `controls`: what the local API and `rkp documents` read and write.
"""
