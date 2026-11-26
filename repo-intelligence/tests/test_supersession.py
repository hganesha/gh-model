"""Per-file supersession in `file.extract` (docs/solution-ir-contract.md, section 10.3).

An extractor naming others in `supersedes_when_parsed` drops their observations and
diagnostics for a file it read (returned at least one observation), and leaves them on a
file it did not read.
"""

from __future__ import annotations

import pytest

from repo_intel import extractors
from repo_intel.compile.ontology import EntityKind
from repo_intel.extractors.base import Extraction, diagnostic, entity, whole_file
from repo_intel.storage.hashing import sha256_hex


class _Generic:
    id = "test.generic"
    version = "1.0.0"

    def matches(self, path: str) -> bool:
        return True

    def extract(self, path, data, blob):
        return Extraction([entity(blob, whole_file(data), EntityKind.PROGRAM, {"platform": "sql", "name": path}, path)], diagnostics=[diagnostic(self.id, "warning", "generic note")])


class _Specific:
    id = "test.specific"
    version = "1.0.0"
    supersedes_when_parsed = ("test.generic",)

    def matches(self, path: str) -> bool:
        return True

    def extract(self, path, data, blob):
        if b"SPECIFIC" not in data:
            return Extraction(diagnostics=[diagnostic(self.id, "info", "not mine")], parsed=False)
        return Extraction([entity(blob, whole_file(data), EntityKind.PROGRAM, {"platform": "plsql", "name": path}, path)])


@pytest.fixture
def fakes(monkeypatch):
    found = [_Generic(), _Specific()]
    monkeypatch.setattr(extractors, "EXTRACTORS", sorted(found, key=lambda e: e.id))
    return {e.id: extractors.extractor_version(e) for e in found}


def _run(data: bytes, wanted: dict[str, str], path: str = "db/x.sql") -> dict:
    return extractors._file_extract({"path": path, "blob": data}, {"parse": True, "extractors": wanted})


def _platforms(summary: dict) -> list[str]:
    return [o["payload"]["natural_key"]["platform"] for o in summary["observations"] if o["kind"] == "entity"]


def _messages(summary: dict) -> list[str]:
    return [d["message"] for d in summary["diagnostics"]]


def test_a_superseding_extractor_that_reads_nothing_leaves_the_generic_output(fakes):
    out = _run(b"select 1 from t;", fakes)
    assert _platforms(out) == ["sql"]
    assert sorted(_messages(out)) == ["generic note", "not mine"]
    assert out["parsed"] is True


def test_a_superseding_extractor_that_reads_the_file_drops_the_generic_output(fakes):
    out = _run(b"SPECIFIC procedure", fakes)
    assert _platforms(out) == ["plsql"]
    assert _messages(out) == [], "the generic extractor's diagnostics go with its observations"


def test_supersession_only_applies_to_extractors_that_ran(fakes):
    out = _run(b"SPECIFIC procedure", {"test.specific": fakes["test.specific"]})
    assert _platforms(out) == ["plsql"]
    out = _run(b"SPECIFIC procedure", {"test.generic": fakes["test.generic"]})
    assert _platforms(out) == ["sql"]


def test_output_is_the_same_run_after_run(fakes):
    data = b"SPECIFIC procedure"
    assert _run(data, fakes) == _run(data, fakes)


def test_plsql_supersedes_the_generic_sql_lineage_on_an_oracle_sql_file():
    from repo_intel.platforms.plsql.source import PlsqlSource

    assert PlsqlSource.supersedes_when_parsed == ("data.sql_lineage",)
    path = "db/load_orders.sql"
    data = b"CREATE OR REPLACE PROCEDURE load_orders AS\nBEGIN\n  INSERT INTO orders_hist SELECT * FROM orders;\nEND load_orders;\n/\n"
    wanted = extractors.applicable_extractors(path)
    assert {"plsql.source", "data.sql_lineage"} <= set(wanted)
    out = extractors._file_extract({"path": path, "blob": data}, {"parse": True, "extractors": wanted})
    programs = [o["payload"]["natural_key"] for o in out["observations"] if o["kind"] == "entity" and o["payload"]["kind"] == "Program"]
    assert programs == [{"platform": "oracle", "name": "load_orders"}], "one Program, from the plsql pack only"
    assert sha256_hex(data) == out["blob_sha256"]


def test_a_plain_sql_file_keeps_the_generic_sql_lineage():
    path = "etl/load.sql"
    data = b"INSERT INTO mart.orders SELECT * FROM stage.orders;\n"
    out = extractors._file_extract({"path": path, "blob": data}, {"parse": True, "extractors": extractors.applicable_extractors(path)})
    programs = [o["payload"]["natural_key"] for o in out["observations"] if o["kind"] == "entity" and o["payload"]["kind"] == "Program"]
    assert programs == [{"platform": "sql", "name": path}]
