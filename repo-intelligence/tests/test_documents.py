"""Designated documents (contract 9.1): the rkp-documents/1 file, the readers, the four
features, rules through a scripted model, coverage, tiers, the local API and the CLI.

Nothing undesignated is read; requirements, schemas and code lists are deterministic; a
model only names rules, behind the business_rules switch, and an owner confirms or rejects
them with a decisions override.
"""

import io
import json
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from conftest import REPO_ID
from repo_intel import cli
from repo_intel.compile.contracts import entity_id
from repo_intel.compile.fixtures import FIXTURES_DIR, materialize
from repo_intel.compile.pipeline import compile_commit, load_shards
from repo_intel.decisions.gateway import Gateway, ScriptedLlm
from repo_intel.decisions.overrides import DecisionOverride, OverrideDocument, Overrides
from repo_intel.documents import controls, features, readers, steps
from repo_intel.documents import format as fmt
from repo_intel.documents import rules as doc_rules
from repo_intel.knowledge import bundle, local_api
from repo_intel.knowledge.bundle import _architecture_shards
from repo_intel.storage.store import ObjectStore
from repo_intel.workspace.settings import CompileSettings

F36 = "F36-designated-documents"
BASE = FIXTURES_DIR / F36 / "base"
ON = CompileSettings(business_rules=True)


def _git_env():
    return {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
            "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z", "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"}


def _commit(repo: Path, files: dict[str, str | bytes], message: str = "x") -> str:
    for name, content in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_bytes(content if isinstance(content, bytes) else content.encode())
    if not (repo / ".git").exists():
        subprocess.run(["git", "-C", str(repo), "init", "-q", "-b", "main"], check=True, env=_git_env())
    for cmd in (["add", "-A"], ["commit", "-q", "--allow-empty", "-m", message]):
        subprocess.run(["git", "-C", str(repo), *cmd], check=True, env=_git_env())
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()


def _designation(*entries) -> str:
    return json.dumps({"schema": fmt.SCHEMA, "documents": list(entries)})


def _entities(shards, kind):
    return [e for s in shards.values() for e in s["entities"] if e["kind"] == kind]


def _assertions(shards, predicate):
    return [a for s in shards.values() for a in s["assertions"] if a["predicate"] == predicate]


def _coverage(store, result, domain="documents"):
    return [c for c in store.get_json(result.manifest.coverage_ref) if c["domain"] == domain]


@pytest.fixture(scope="module")
def f36(tmp_path_factory):
    """F36 materialized: (repo path, commit SHAs)."""
    root = tmp_path_factory.mktemp("f36")
    return root / "repo", materialize(F36, root / "repo")


# ---------------------------------------------------------------- the designation file


def test_load_keeps_good_entries_and_says_why_others_are_ignored():
    data = _designation(
        {"path": "./docs/a.md", "features": ["rules", "requirements", "rules"], "note": "  signed off  "},
        {"path": "docs/b.md", "features": ["bogus", "rules"]},
        {"path": "docs/c.md", "features": []},
        {"path": "/etc/passwd", "features": ["rules"]},
        {"path": "docs/../../x.md", "features": ["rules"]},
        "not an entry",
    ).encode()
    entries, problems = fmt.load(data)
    assert entries == [{"path": "docs/a.md", "features": ["requirements", "rules"], "note": "signed off"}]
    messages = [p["message"] for p in problems]
    assert any("unknown feature bogus" in m for m in messages)
    assert any("at least one" in m for m in messages)
    assert sum("inside the repository" in m for m in messages) == 2
    assert any("not an object" in m for m in messages)

    assert fmt.load(b'{"schema": "rkp-documents/2", "documents": []}')[0] == []
    assert "not JSON" in fmt.load(b"{nope")[1][0]["message"]


def test_globs_match_within_and_across_segments_and_features_unite():
    assert fmt.matches("docs/*.md", "docs/a.md") and not fmt.matches("docs/*.md", "docs/sub/a.md")
    assert fmt.matches("specs/**/*.docx", "specs/a.docx") and fmt.matches("specs/**/*.docx", "specs/x/y/a.docx")
    assert fmt.matches("docs/?.md", "docs/a.md") and not fmt.matches("docs/?.md", "docs/ab.md")
    assert fmt.matches("docs/a.md", "docs/a.md") and not fmt.matches("docs/a.md", "docs/a.mdx")
    resolved, problems = fmt.resolve(
        [{"path": "docs/*.md", "features": ["requirements"]}, {"path": "docs/a.md", "features": ["rules"]}, {"path": "none/**", "features": ["schema"]}],
        ["docs/a.md", "docs/b.md", "src/x.py"])
    assert resolved == {"docs/a.md": ["requirements", "rules"], "docs/b.md": ["requirements"]}
    assert [p["path"] for p in problems] == ["none/**"]


def test_written_designations_are_sorted_merged_and_validated():
    text = fmt.dump([{"path": "z.md", "features": ["schema", "code_lists"]}, {"path": "a.md", "features": ["rules"], "note": "n"}, {"path": "z.md", "features": ["rules"]}])
    assert json.loads(text) == {"schema": fmt.SCHEMA, "documents": [
        {"path": "a.md", "features": ["rules"], "note": "n"}, {"path": "z.md", "features": ["code_lists", "rules", "schema"]}]}
    assert text.index('"path"') < text.index('"features"'), "entries keep path first"
    for body in ({"documents": [{"path": "a.md", "features": ["nope"]}]}, {"documents": [{"path": "a.md", "features": ["rules"], "extra": 1}]},
                 {"documents": "a.md"}, {"documents": [{"path": "a.md", "features": ["rules"], "note": "x" * 501}]}):
        with pytest.raises(fmt.DesignationError):
            fmt.validate_for_save(body)


# ---------------------------------------------------------------- readers


def test_markdown_blocks_carry_kind_heading_path_cells_and_exact_spans():
    data = ("# Orders\n\nIntro line one\nline two.\n\n## Rules\n\n- first item\n  continued\n* second\n\n"
            "| Code | Meaning |\n|---|---|\n| A | Approved |\n\n```mermaid\nerDiagram\n```\n\nTitle\n=====\n\nlast\n").encode()
    blocks = readers.read_markdown(data)
    assert [(b["kind"], b["heading_path"]) for b in blocks] == [
        ("heading", []), ("paragraph", ["Orders"]), ("heading", ["Orders"]), ("item", ["Orders", "Rules"]), ("item", ["Orders", "Rules"]),
        ("table_row", ["Orders", "Rules"]), ("table_row", ["Orders", "Rules"]), ("code", ["Orders", "Rules"]), ("heading", []), ("paragraph", ["Title"])]
    assert blocks[1]["text"] == "Intro line one line two." and blocks[3]["text"] == "first item continued"
    assert blocks[5]["cells"] == ["Code", "Meaning"] and blocks[6]["cells"] == ["A", "Approved"] and blocks[5]["table"] == blocks[6]["table"]
    assert blocks[7]["info"] == "mermaid" and blocks[7]["text"] == "erDiagram"
    for b in blocks:
        assert data[b["at"][0]:b["at"][1]].decode().strip(), b


def test_text_csv_and_html_readers():
    text = readers.read_text(b"POLICY\n======\n\nOrders must be approved.\n\n- one\n- two\n")
    assert [b["kind"] for b in text] == ["heading", "paragraph", "item", "item"]
    assert text[1]["heading_path"] == ["POLICY"]

    data = b'code;meaning\nA;"Approved, final"\n\nB;"Two\nlines"\n'
    rows = readers.read_csv(data, "ref/status.csv")
    assert [r["cells"] for r in rows] == [["code", "meaning"], ["A", "Approved, final"], ["B", "Two lines"]]
    assert rows[0]["heading_path"] == ["status"]
    assert data[rows[2]["at"][0]:rows[2]["at"][1]] == b'B;"Two\nlines"'

    page = ("<html><head><title>x</title><style>p{}</style></head><body><h1>Café rules</h1>"
            "<p>Orders <b>must</b> be approved.</p><script>var x = 'must';</script>"
            "<ul><li>first</li><li>second</li></ul><table><tr><th>Code</th><th>Meaning</th></tr><tr><td>A</td><td><p>Approved</p></td></tr></table>"
            "<div>loose text</div></body></html>").encode()
    blocks = readers.read_html(page)
    assert [(b["kind"], b["text"]) for b in blocks] == [
        ("heading", "Café rules"), ("paragraph", "Orders must be approved."), ("item", "first"), ("item", "second"),
        ("table_row", "Code | Meaning"), ("table_row", "A | Approved"), ("paragraph", "loose text")]
    assert blocks[1]["heading_path"] == ["Café rules"]
    p = blocks[1]
    assert page[p["at"][0]:p["at"][1]] == b"<p>Orders <b>must</b> be approved.</p>", "byte offsets survive non-ASCII text"


def test_docx_reader_reads_headings_tables_and_exact_spans_from_stored_members():
    data = (BASE / "docs" / "data-dictionary.docx").read_bytes()
    blocks = readers.read_docx(data)
    headings = [b["text"] for b in blocks if b["kind"] == "heading"]
    assert headings == ["Order data dictionary", "orders", "customers"]
    rows = [b for b in blocks if b["kind"] == "table_row"]
    assert rows[0]["cells"] == ["Field", "Type", "Nullable", "Key", "Description"] and rows[0]["heading_path"] == ["Order data dictionary", "orders"]
    assert rows[3]["cells"][:2] == ["status", "varchar(20)"]
    assert b"varchar(20)" in data[rows[3]["at"][0]:rows[3]["at"][1]], "a stored member gives the row's own bytes"
    assert len({r["table"] for r in rows}) == 2


def _docx(paragraphs: str, compression=zipfile.ZIP_DEFLATED) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=compression) as zf:
        zf.writestr("word/document.xml", '<w:document xmlns:w="w"><w:body>' + paragraphs + "</w:body></w:document>")
    return buf.getvalue()


def test_docx_lists_outline_levels_and_compressed_members():
    xml = ('<w:p><w:pPr><w:outlineLvl w:val="1"/></w:pPr><w:r><w:t>Limits</w:t></w:r></w:p>'
           '<w:p><w:pPr><w:numPr><w:ilvl w:val="0"/></w:numPr></w:pPr><w:r><w:t>Orders &amp; returns </w:t></w:r><w:r><w:t>must be signed.</w:t></w:r></w:p>'
           '<w:p><w:r><w:t>plain</w:t></w:r><w:r><w:tab/></w:r><w:r><w:t>text</w:t></w:r></w:p>')
    data = _docx(xml)
    blocks = readers.read_docx(data)
    assert [(b["kind"], b["text"], b["heading_path"]) for b in blocks] == [
        ("heading", "Limits", []), ("item", "Orders & returns must be signed.", ["Limits"]), ("paragraph", "plain text", ["Limits"])]
    assert len({tuple(b["at"]) for b in blocks}) == 1, "a compressed member's span is the member, for every block"


def test_xlsx_reader_uses_cached_values_and_never_evaluates_formulas():
    data = (BASE / "docs" / "order-codes.xlsx").read_bytes()
    blocks, counts = readers.read_xlsx(data)
    rows = [b for b in blocks if b["kind"] == "table_row"]
    status = [r for r in rows if r["sheet"] == "Order status"]
    assert [r["cells"] for r in status] == [["Code", "Meaning"], ["D", "Draft"], ["S", "Submitted"], ["A", "Approved"], ["X", "Cancelled"]]
    assert status[1]["cell"] == "A2" and status[0]["heading_path"] == ["Order status"]
    totals = [r["cells"] for r in rows if r["sheet"] == "Counts"]
    assert totals == [["Code", "Count"], ["D", "12"], ["S", "7"], ["A"]], "cached values only; the formula with none reads as empty"
    assert counts == {"formulas_without_value": 1}
    for r in status:
        assert data[r["at"][0]:r["at"][1]].startswith(b"<row")


def test_pdf_images_and_broken_archives_are_unreadable_with_the_export_named():
    assert readers.export_needed("pdf") == "Markdown, DOCX or text"
    assert readers.export_needed(None) == "Markdown, DOCX or text"
    assert readers.export_needed("docx") is None
    with pytest.raises(readers.Unreadable):
        readers.read("a.pdf", "pdf", b"%PDF")
    with pytest.raises(readers.Unreadable, match="not a zip"):
        readers.read("a.docx", "docx", b"not a zip")
    with pytest.raises(readers.Unreadable, match="no word/document.xml"):
        readers.read("a.docx", "docx", _docx("")[:0] or _zip_without_document())


def _zip_without_document() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("other.xml", "<x/>")
    return buf.getvalue()


def test_zip_members_are_secret_masked_before_reading():
    secret = "AKIAIOSFODNN7EXAMPLE"
    data = _docx(f"<w:p><w:r><w:t>The key {secret} must not be shared.</w:t></w:r></w:p>")
    [block] = readers.read_docx(data)
    assert secret not in block["text"] and "must not be shared" in block["text"]


# ---------------------------------------------------------------- features


def _blocks(markdown: str):
    return readers.read_markdown(markdown.encode())


def test_requirements_need_a_leading_identifier_or_a_modal():
    found, used = features.requirements(_blocks(
        "# Spec\n\n- **REQ-7**: Orders are numbered.\n- [FR-12] The desk shall print a slip.\n"
        "- See REQ-7 for numbering.\n- The total must not be negative and must use `orders.amount`.\n"
        "- REQ-7 again, the same requirement.\n\nPlain description.\n"))
    by_name = {r["name"]: r for r in found}
    assert set(by_name) == {"req-7", "fr-12", "the-total-must-not-be-negative-and-must"}
    assert by_name["req-7"]["identifier"] == "REQ-7" and "modality" not in by_name["req-7"] and by_name["req-7"]["statement"] == "Orders are numbered."
    assert by_name["fr-12"]["modality"] == "shall" and by_name["fr-12"]["section"] == ["Spec"]
    assert by_name["the-total-must-not-be-negative-and-must"]["modality"] == "must not"
    assert by_name["the-total-must-not-be-negative-and-must"]["mentions"]["quoted"] == ["orders.amount"]
    assert len(by_name["req-7"]["blocks"]) == 2, "a repeated identifier adds evidence, not a second requirement"
    assert sorted(used) == [1, 2, 4, 5], "the reference in passing and the description are not used"
    assert len(features.slug("one two three four five six seven eight nine ten")) <= 60
    assert features.slug("a " * 40) == "-".join(["a"] * 8)


def test_schema_tables_need_a_name_and_a_type_column_under_a_heading_or_with_an_entity():
    md = ("## orders\n\n| Column | Data type | Required | Description |\n|---|---|---|---|\n| id | int | yes | key |\n| note | text | no | free |\n\n"
          "| Entity | Field | Type |\n|---|---|---|\n| lines | qty | int |\n| lines | sku | text |\n\n"
          "| Field | Meaning |\n|---|---|\n| x | not a schema |\n\n"
          "```mermaid\nerDiagram\n  CUSTOMER {\n    string id PK\n    string email \"contact\"\n  }\n```\n")
    found, used = features.schemas(_blocks(md), "d.md")
    by_entity = {s["entity"]: s for s in found}
    assert set(by_entity) == {"orders", "lines", "CUSTOMER"}
    assert by_entity["orders"]["name"] == "d.md#orders"
    id_field, note = by_entity["orders"]["fields"]
    assert id_field == {"name": "id", "type": "int", "nullable": False, "description": "key", "block": id_field["block"]}
    assert note["nullable"] is True, "a required column reads the other way round"
    assert [f["name"] for f in by_entity["lines"]["fields"]] == ["qty", "sku"]
    assert by_entity["CUSTOMER"]["fields"] == [{"name": "email", "type": "string", "description": "contact", "block": 9}, {"name": "id", "type": "string", "key": "PK", "block": 9}]
    assert not {7, 8} & set(used), "a two-column field table with no type is not read"


def test_code_lists_are_two_column_code_and_meaning_tables():
    md = ("## Order status\n\n| Code | Meaning |\n|---|---|\n| D | Draft |\n| A | Approved |\n\n"
          "| Code | Label |\n|---|---|\n| X | Other |\n\n## Other\n\n| Code | Count |\n|---|---|\n| D | 4 |\n\n| Code | Meaning | Since |\n|---|---|---|\n| Z | z | 2020 |\n")
    found, used = features.code_lists(_blocks(md), "d.md")
    assert [(c["name"], c["values"]) for c in found] == [
        ("d.md#order-status", [{"code": "D", "label": "Draft"}, {"code": "A", "label": "Approved"}]),
        ("d.md#order-status~2", [{"code": "X", "label": "Other"}])]
    many = "## Big\n\n| Code | Meaning |\n|---|---|\n" + "".join(f"| c{i} | m{i} |\n" for i in range(features.MAX_VALUES + 5))
    [big], _ = features.code_lists(_blocks(many), "d.md")
    assert len(big["values"]) == features.MAX_VALUES and big["omitted"] == 5


def test_rule_candidates_are_blocks_with_a_rule_signal():
    found, omitted = features.rule_candidates(_blocks(
        "# Pricing\n\n- A discount cannot exceed 15%.\n- Orders over 1000 must be approved.\n- If the customer is in the EU then VAT applies.\n"
        "- Users must not share accounts.\n- Prices are in euros.\n- Refunds only if the goods are returned, at least 2 days later.\n"))
    assert [(c["id"], c["signals"]) for c in found] == [
        ("d1", ["cannot", "percentage", "threshold"]), ("d2", ["must", "threshold"]), ("d3", ["if_then"]), ("d4", ["must_not"]),
        ("d5", ["only_if", "at_least"])]
    assert omitted == 0 and found[0]["section"] == ["Pricing"]


# ---------------------------------------------------------------- the compile


def test_f36_designated_documents_produce_entities_and_undesignated_ones_nothing(f36, tmp_path):
    repo, shas = f36
    store = ObjectStore(tmp_path / "store", "t")
    result = compile_commit(store, repo, REPO_ID, shas[0])
    shards = load_shards(store, result.manifest)
    documents = {e["attributes"].get("document") for kind in ("Requirement", "DataSchema", "CodeList") for e in _entities(shards, kind)}
    assert documents == {"docs/requirements.md", "docs/data-dictionary.docx", "docs/order-codes.xlsx"}
    fields = [e for e in _entities(shards, "Field") if "#" in e["natural_key"]["table"]]
    assert {e["natural_key"]["table"] for e in fields} == {"docs/data-dictionary.docx#orders", "docs/data-dictionary.docx#customers"}
    assert not _entities(shards, "BusinessRule"), "rules need the business_rules switch"

    # Evidence cites the document's bytes, and the global shard holds it.
    by_id = {ev["id"]: ev for s in shards.values() for ev in s["evidence"]}
    data = (BASE / "docs" / "requirements.md").read_bytes()
    req1 = next(e for e in _entities(shards, "Requirement") if e["natural_key"]["name"] == "req-1")
    span = by_id[req1["evidence_ids"][0]]["span"]
    assert data[span["start_byte"]:span["end_byte"]].startswith(b"- REQ-1:")
    assert {a["evidence_class"] for a in _assertions(shards, "APPLIES_TO") if a["subject"] == req1["id"]} == {"E4"}
    assert {(a["evidence_class"], a["epistemic_status"]) for a in _assertions(shards, "DEFINED_IN") if a["subject"] == req1["id"]} == {("E2", "supported")}

    [cov] = _coverage(store, result)
    assert cov["scope"] == "@root" and 0 < cov["numerator"] < cov["denominator"]
    detail = cov["detail"]
    assert detail["documents"] == 4 and detail["rule_lifting"] == "off"
    assert detail["by_feature"]["rules"] == {"blocks": 0, "candidates": 3, "entities": 0}, "with the switch off, candidates are only counted"
    assert detail["by_feature"]["requirements"]["entities"] == 4 and detail["by_feature"]["schema"]["fields"] == 7
    assert detail["unreadable"] == [{"path": "docs/scanned-policy.pdf", "reason": "excluded from the snapshot as binary", "export": "Markdown, DOCX or text"}]
    assert detail["reading"] == {"formulas_without_value": 1}
    assert detail["designation"]["path"] == ".rkp/documents.json" and detail["designation"]["entries"] == 5
    unknowns = [u for u in store.get_json(result.manifest.unknowns_ref) if u["coverage_ref"] == "documents:@root"]
    assert [u["predicate"] for u in unknowns] == ["document_read"] and "scanned-policy.pdf" in unknowns[0]["reason"]


def test_the_designation_file_is_a_compile_input(tmp_path):
    repo = tmp_path / "repo"
    files = {"docs/a.md": "- REQ-1: Orders must be numbered.\n", ".rkp/documents.json": _designation({"path": "docs/a.md", "features": ["requirements"]})}
    store = ObjectStore(tmp_path / "store", "t")
    first = compile_commit(store, repo, REPO_ID, _commit(repo, files))
    # Only the note changes: the entities are the same, the snapshot is not.
    second = compile_commit(store, repo, REPO_ID, _commit(repo, {".rkp/documents.json": _designation({"path": "docs/a.md", "features": ["requirements"], "note": "checked"})}))
    assert first.manifest.rkp_hash != second.manifest.rkp_hash
    assert {e["id"] for e in _entities(load_shards(store, first.manifest), "Requirement")} == {e["id"] for e in _entities(load_shards(store, second.manifest), "Requirement")}
    # An unchanged document is not read again.
    assert second.stats.hits["documents.read"] == 1
    # Without the file, nothing is read and no documents coverage appears.
    subprocess.run(["git", "-C", str(repo), "rm", "-q", ".rkp/documents.json"], check=True, env=_git_env())
    third = compile_commit(store, repo, REPO_ID, _commit(repo, {}))
    assert not _entities(load_shards(store, third.manifest), "Requirement") and not _coverage(store, third)


def test_designation_problems_are_unknowns_in_the_documents_coverage(tmp_path):
    repo = tmp_path / "repo"
    files = {"docs/a.md": "Text.\n", "docs/b.yaml": "a: 1\n", ".rkp/documents.json": _designation(
        {"path": "docs/missing/*.md", "features": ["rules"]}, {"path": "docs/a.md", "features": ["typo"]}, {"path": "docs/b.yaml", "features": ["schema"]})}
    store = ObjectStore(tmp_path / "store", "t")
    result = compile_commit(store, repo, REPO_ID, _commit(repo, files))
    reasons = sorted(u["reason"] for u in store.get_json(result.manifest.unknowns_ref) if u["coverage_ref"].startswith("documents:"))
    assert len(reasons) == 3
    assert any("matches no file" in r for r in reasons) and any("unknown feature typo" in r for r in reasons) and any("docs/b.yaml" in r and "yaml is not read" in r for r in reasons)
    [cov] = _coverage(store, result)
    assert cov["numerator"] == cov["denominator"] == 0 and cov["detail"]["designation"]["problems"] == 2


# ---------------------------------------------------------------- rules through a scripted model


class DocumentModel:
    def __init__(self):
        self.calls = []

    def __call__(self, data):
        self.calls.append(data)
        if data["scope"] != "document":
            return {"rules": []} if data["scope"] == "owner" else {}
        rules = []
        for c in data["features"]["candidates"]:
            if "15%" in c["text"]:
                rules.append({"name": "Discount cap", "statement": "A discount is at most 15 percent of the order amount.", "rule_kind": "validation", "candidate_ids": [c["id"]]})
            elif "1000" in c["text"]:
                rules.append({"name": "large-orders-need-a-manager", "statement": "Orders over 1000 need a manager's approval before submission.", "rule_kind": "decision", "candidate_ids": [c["id"]]})
        rules.append({"name": "made-up", "statement": "Cites nothing given.", "rule_kind": "decision", "candidate_ids": ["d99"]})
        return {"rules": rules}


def _gateway(model, allowed=("scripted",)):
    llm = ScriptedLlm(model)
    return Gateway({}, frozenset(allowed), {"llm-default": llm}), llm


def test_rules_are_lifted_only_with_the_switch_and_an_allowed_model(f36, tmp_path):
    repo, shas = f36
    model = DocumentModel()
    gw, llm = _gateway(model)
    store = ObjectStore(tmp_path / "store", "t")
    off = compile_commit(store, repo, REPO_ID, shas[0], gateway=gw)
    assert not _entities(load_shards(store, off.manifest), "BusinessRule") and not model.calls

    on = compile_commit(store, repo, REPO_ID, shas[0], gateway=gw, policy=ON)
    shards = load_shards(store, on.manifest)
    rules = {e["natural_key"]["name"]: e for e in _entities(shards, "BusinessRule")}
    assert set(rules) == {"discount-cap", "large-orders-need-a-manager"}, "a rule citing an id not given is dropped"
    cap = rules["discount-cap"]
    assert cap["natural_key"] == {"scope": "docs/pricing-rules.md", "name": "discount-cap"}
    assert cap["attributes"]["status"] == "hypothesis" and cap["attributes"]["trust_label"] == "untrusted_generated" and cap["attributes"]["rule_kind"] == "validation"
    defined = [a for a in _assertions(shards, "DEFINED_IN") if a["subject"] == cap["id"]]
    assert [(a["object"], a["evidence_class"], a["assertion_kind"], a["epistemic_status"]) for a in defined] == [
        (entity_id(REPO_ID, "Artifact", {"path": "docs/pricing-rules.md"}), "E5", "hypothesis", "inferred")]
    [request] = [c for c in model.calls if c["scope"] == "document"]
    assert request["trust"] == "untrusted_data" and request["features"]["document"] == "docs/pricing-rules.md"
    assert [c["id"] for c in request["features"]["candidates"]] == ["d1", "d2", "d3"]
    [cov] = _coverage(store, on)
    assert cov["detail"]["rule_lifting"] == "on" and cov["detail"]["by_feature"]["rules"] == {"blocks": 2, "candidates": 3, "entities": 2}
    assert cov["numerator"] == _coverage(store, off)[0]["numerator"] + 2

    # Egress not allowed for the provider: the decision abstains and no rule is made.
    denied, _ = _gateway(DocumentModel(), allowed=())
    shut = compile_commit(ObjectStore(tmp_path / "store2", "t"), repo, REPO_ID, shas[0], gateway=denied, policy=ON)
    store2 = ObjectStore(tmp_path / "store2", "t")
    assert not _entities(load_shards(store2, shut.manifest), "BusinessRule")
    ledger = [store2.get_json(e["digest"]) for e in store2.get_json(shut.manifest.ledger_entries_ref)]
    assert [d["abstain_reason"] for d in ledger if d["spec"].startswith("dm.document-rules")] == ["egress_denied"]


def test_owner_confirms_and_rejects_document_rules(f36, tmp_path):
    repo, shas = f36
    gw, llm = _gateway(DocumentModel())
    store = ObjectStore(tmp_path / "store", "t")
    first = compile_commit(store, repo, REPO_ID, shas[0], gateway=gw, policy=ON)
    rules = {e["natural_key"]["name"]: e["id"] for e in _entities(load_shards(store, first.manifest), "BusinessRule")}
    calls = len(llm.calls)
    overrides = Overrides.of(OverrideDocument(decisions=[
        DecisionOverride(spec="dm.document-rules", scope_entity=rules["discount-cap"], label="confirmed"),
        DecisionOverride(spec="dm.document-rules", scope_entity=rules["large-orders-need-a-manager"], label="rejected", reason="The limit moved to the pricing service."),
    ]))
    second = compile_commit(store, repo, REPO_ID, shas[0], gateway=gw, policy=ON, overrides=overrides)
    assert len(llm.calls) == calls, "confirming asks the model nothing"
    shards = load_shards(store, second.manifest)
    kept = {e["natural_key"]["name"]: e for e in _entities(shards, "BusinessRule")}
    assert set(kept) == {"discount-cap"}, "a rejected rule leaves the snapshot"
    cap = kept["discount-cap"]
    assert cap["attributes"]["status"] == "confirmed" and cap["attributes"]["confirmation_ref"].startswith("dec_")
    [edge] = [a for a in _assertions(shards, "DEFINED_IN") if a["subject"] == cap["id"]]
    assert (edge["epistemic_status"], edge["verification_ref"], edge["decision_ref"]) == ("supported", "owner_override", cap["attributes"]["confirmation_ref"])
    conflicts = [c for c in store.get_json(second.manifest.conflicts_ref) if c["subject_ids"] == [rules["large-orders-need-a-manager"]]]
    assert len(conflicts) == 1 and conflicts[0]["preferred"] == "owner_override:rejected"
    assert _coverage(store, second)[0]["detail"]["by_feature"]["rules"]["entities"] == 1


def test_rule_model_features_send_candidates_only():
    doc = {"path": "d.md", "candidates": [{"id": f"d{i}", "kind": "item", "section": ["A", "B"], "text": "t", "evidence_id": "ev", "block": i, "applies_to": [], "at": [0, 1], "signals": ["must"]} for i in range(1, 45)], "omitted_candidates": 3}
    f = doc_rules.model_features(doc)
    assert len(f["candidates"]) == doc_rules.MAX_SENT and f["omitted_candidates"] == 44 - doc_rules.MAX_SENT + 3
    assert set(f["candidates"][0]) == {"id", "kind", "section", "text"} and f["candidates"][0]["section"] == "A > B"


# ---------------------------------------------------------------- tiers


def test_document_entities_are_in_the_architecture_tier(f36, tmp_path):
    repo, shas = f36
    store = ObjectStore(tmp_path / "store", "t")
    result = compile_commit(store, repo, REPO_ID, shas[3])
    shards = load_shards(store, result.manifest)
    tier = _architecture_shards(shards, store.get_json(result.manifest.conflicts_ref))
    kinds = {e["kind"] for s in tier.values() for e in s["entities"]}
    assert {"Requirement", "DataSchema", "Field", "CodeList"} <= kinds
    full = {e["id"] for s in shards.values() for e in s["entities"] if e["kind"] in ("Requirement", "DataSchema", "CodeList")}
    assert full <= {e["id"] for s in tier.values() for e in s["entities"]}


# ---------------------------------------------------------------- owner controls: the local API and the CLI


@pytest.fixture
def compiled_f36(tmp_path):
    path = tmp_path / "repo"
    materialize(F36, path)
    bundle.compile_bundle(path)
    return path


def test_api_lists_documents_candidates_and_problems(compiled_f36):
    api = local_api.LocalApi(compiled_f36)
    status, answer = api.handle("GET", "repositories/local/repo/documents")
    assert status == 200 and set(answer) == {"documents", "candidates", "problems", "version"}
    # The fixture's last commit is checked out: the glossary is designated there.
    assert [d["path"] for d in answer["documents"]] == [
        "docs/*.xlsx", "docs/data-dictionary.docx", "docs/glossary.md", "docs/pricing-rules.md", "docs/requirements.md", "docs/scanned-policy.pdf"]
    assert answer["documents"][1] == {"path": "docs/data-dictionary.docx", "features": ["schema"], "note": "kept by the warehouse team"}
    candidates = {c["path"]: c["language"] for c in answer["candidates"]}
    assert candidates["docs/data-dictionary.docx"] == "docx" and candidates["docs/order-codes.xlsx"] == "xlsx" and candidates["docs/scanned-policy.pdf"] == "pdf"
    assert candidates["docs/meeting-notes.md"] == "markdown" and not any(p.startswith(".rkp/") for p in candidates)
    assert answer["problems"] == [] and answer["version"] > 0


def test_api_saves_validated_designations(compiled_f36):
    api = local_api.LocalApi(compiled_f36)
    _, before = api.handle("GET", "repositories/local/repo/documents")
    body = {"documents": [*before["documents"], {"path": "docs/glossary.md", "features": ["code_lists"]}, {"path": "nowhere/*.md", "features": ["rules"]}], "expected_version": before["version"]}
    status, saved = api.handle("PUT", "repositories/local/repo/documents", body)
    assert status == 200 and "docs/glossary.md" in [d["path"] for d in saved["documents"]]
    assert [p["path"] for p in saved["problems"]] == ["nowhere/*.md"]
    written = json.loads((compiled_f36 / ".rkp" / "documents.json").read_text())
    assert [d["path"] for d in written["documents"]] == sorted(d["path"] for d in written["documents"])
    assert bundle.bundle_status(compiled_f36, working_tree=True)["state"] == "stale", "the change takes effect at the next compile"

    status, stale = api.handle("PUT", "repositories/local/repo/documents", body)
    assert status == 409 and stale["code"] == "stale"
    status, invalid = api.handle("PUT", "repositories/local/repo/documents", {"documents": [{"path": "docs/a.md", "features": ["everything"]}]})
    assert status == 422 and "unknown feature" in invalid["message"]
    assert json.loads((compiled_f36 / ".rkp" / "documents.json").read_text()) == written, "nothing half-valid is written"


def test_api_answers_before_the_first_compile(tmp_path):
    repo = tmp_path / "repo"
    _commit(repo, {"README.md": "x\n"})
    status, answer = local_api.LocalApi(repo).handle("GET", "repositories/local/repo/documents")
    assert status == 200 and answer == {"documents": [], "candidates": [], "problems": [], "version": 0}


def test_cli_lists_sets_and_clears_designations(compiled_f36, capsys, monkeypatch):
    repo = compiled_f36
    assert cli.main(["documents", "list", "--repo", str(repo), "--json"]) == 0
    [line] = capsys.readouterr().out.splitlines()
    event = json.loads(line)
    assert event["event"] == "result" and len(event["documents"]) == 6 and event["candidates"]

    monkeypatch.chdir(repo / "docs")
    assert cli.main(["documents", "set", "glossary.md", "--feature", "code_lists", "--feature", "requirements", "--repo", str(repo)]) == 0
    out = capsys.readouterr().out
    assert "docs/glossary.md: code_lists, requirements" in out and "next compile" in out
    assert cli.main(["documents", "set", "docs/pricing-rules.md", "--feature", "rules", "--repo", str(repo), "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert next(d for d in result["documents"] if d["path"] == "docs/pricing-rules.md")["note"] == "signed off by finance", "a note is kept"
    assert cli.main(["documents", "set", "docs/requirements.md", "--clear", "--repo", str(repo)]) == 0
    capsys.readouterr()
    paths = [d["path"] for d in json.loads((repo / ".rkp" / "documents.json").read_text())["documents"]]
    assert "docs/requirements.md" not in paths and "docs/glossary.md" in paths

    assert cli.main(["documents", "list", "--repo", str(repo)]) == 0
    text = capsys.readouterr().out
    assert "docs/requirements.md (markdown)" in text, "an undesignated candidate is offered"
    with pytest.raises(SystemExit) as bad:
        cli.main(["documents", "set", "docs/a.md", "--feature", "everything", "--repo", str(repo)])
    assert bad.value.code == 2
    assert cli.main(["documents", "set", "../outside.md", "--feature", "rules", "--repo", str(repo)]) == 1


def test_link_step_matches_names_exactly_once():
    shards = {"u": {"entities": [
        {"id": "t1", "kind": "Table", "natural_key": {"datastore": "default", "name": "orders"}},
        {"id": "t2", "kind": "Table", "natural_key": {"datastore": "other", "name": "orders"}},
        {"id": "t3", "kind": "Table", "natural_key": {"datastore": "default", "name": "sales.customers"}},
        {"id": "f1", "kind": "Field", "natural_key": {"table": "sales.customers", "name": "id"}},
        {"id": "s1", "kind": "Screen", "natural_key": {"platform": "web", "name": "/orders"}},
        {"id": "e1", "kind": "Endpoint", "natural_key": {"method": "POST", "route": "/api/orders"}},
    ]}}
    names = steps._Names(shards)
    assert names.named({"quoted": ["orders"], "qualified": []}) == [], "two tables named orders: no guess"
    assert names.named({"quoted": ["sales.customers.id", "/orders", "post /api/orders"], "qualified": ["Sales.Customers"]}) == ["e1", "f1", "s1", "t3"]
    assert names.table_in_unit("u", "customers") == "t3" and names.table_in_unit("u", "orders") is None and names.table_in_unit("v", "customers") is None
    assert features.mentions("Use `orders` and sales.customers, not docs/x.md or a.b/c") == {"quoted": ["orders"], "qualified": ["sales.customers"]}
