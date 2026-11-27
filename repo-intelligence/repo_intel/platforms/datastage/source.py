"""The DataStage pack's extractors: `.dsx` exports and `.isx` archives.

`datastage.dsx` reads one export file in either form (`dsx`) and hands its records to
`jobs.Reader`. `datastage.isx` reads an Information Server archive (`istool export`), a zip
file opened in memory from the file's bytes and never extracted to disk, within the same
bounds as the SAS pack's Enterprise Guide reader: at most `MAX_ENTRIES` members,
`MAX_MEMBER` bytes from one member and `MAX_TOTAL` in all. Each member is masked for
secrets before it is parsed, since the snapshot's scan cannot see inside a compressed
archive. A member that is a DataStage export (text or XML) is read like a `.dsx` file;
the manifest under `META-INF/` is skipped; any other member is counted as
`ds:isx_member`, a gap that names what it is not.

Spans of a stored member are exact; those of a compressed member are the member's bytes.
"""

from __future__ import annotations

import io
import zipfile

from repo_intel.platforms.datastage import dsx
from repo_intel.platforms.datastage.jobs import Reader

MAX_ENTRIES = 5000
MAX_MEMBER = 4_000_000
MAX_TOTAL = 16_000_000


def _lineage(base_reader: Reader, data: bytes) -> list:
    from repo_intel.extractors import sql_lineage

    return sql_lineage.lineage_summary(base_reader.blob, data, base_reader.resolved, base_reader.unresolved)


class DsxExport:
    id = "datastage.dsx"
    version = "1.0.0"

    def matches(self, path: str) -> bool:
        return path.lower().endswith(".dsx")

    def extract(self, path: str, data: bytes, blob: str):
        from repo_intel.extractors import base

        try:
            export = dsx.parse(data)
        except dsx.DsxError as exc:
            return base.Extraction(diagnostics=[base.diagnostic(self.id, "warning", f"not read: {exc}")], parsed=False)
        whole = base.whole_file(data)
        exact = export.exact

        def span(a: int, b: int):
            if not exact:
                return whole
            return base.span(data, max(0, min(a, len(data))), max(0, min(b, len(data))))

        reader = Reader(path, blob, base, span, whole, self.id)
        reader.run(export)
        return base.Extraction(reader.items + _lineage(reader, data), reader.diagnostics)


class IsxArchive:
    id = "datastage.isx"
    version = "1.0.0"

    def matches(self, path: str) -> bool:
        return path.lower().endswith(".isx")

    def extract(self, path: str, data: bytes, blob: str):
        from repo_intel.extractors import base
        from repo_intel.ingest import secrets
        from repo_intel.platforms import construct as construct_observation
        from repo_intel.compile.ontology import EvidenceClass

        try:
            zf = zipfile.ZipFile(io.BytesIO(data))
        except (zipfile.BadZipFile, OSError, ValueError):
            return base.Extraction(diagnostics=[base.diagnostic(self.id, "warning", "not a zip archive")], parsed=False)
        items: list = []
        diagnostics: list = []
        resolved = unresolved = 0
        total = 0
        with zf:
            infos = zf.infolist()
            if len(infos) > MAX_ENTRIES:
                return base.Extraction(diagnostics=[base.diagnostic(self.id, "info", f"archive has more than {MAX_ENTRIES} members: not read")], parsed=False)
            for info in sorted(infos, key=lambda i: i.filename):
                if info.is_dir() or info.filename.upper().startswith("META-INF/"):
                    continue
                start, end = _member_range(data, info)
                member_span = base.span(data, start, end)
                if info.flag_bits & 0x1:
                    diagnostics.append(base.diagnostic(self.id, "info", f"member {info.filename} is encrypted: not read"))
                    continue
                stored = info.compress_type == zipfile.ZIP_STORED
                if stored:
                    raw = data[start:min(end, start + MAX_MEMBER + 1)]
                else:
                    try:
                        with zf.open(info) as f:
                            raw = f.read(MAX_MEMBER + 1)
                    except (zipfile.BadZipFile, NotImplementedError, OSError, EOFError, RuntimeError) as exc:
                        diagnostics.append(base.diagnostic(self.id, "warning", f"member {info.filename} cannot be read: {type(exc).__name__}"))
                        continue
                if len(raw) > MAX_MEMBER:
                    diagnostics.append(base.diagnostic(self.id, "info", f"member {info.filename} is larger than {MAX_MEMBER} bytes: not read"))
                    continue
                total += len(raw)
                if total > MAX_TOTAL:
                    diagnostics.append(base.diagnostic(self.id, "info", f"archive holds more than {MAX_TOTAL} bytes of members: the rest is not read"))
                    break
                raw = secrets.mask_for_extraction(raw, secrets.scan(raw))
                if not (dsx.is_text_export(raw) or dsx.is_xml_export(raw)):
                    items.append(construct_observation(blob, member_span, "ds:isx_member", False, evidence_class=EvidenceClass.E1))
                    continue
                try:
                    export = dsx.parse(raw)
                except dsx.DsxError as exc:
                    diagnostics.append(base.diagnostic(self.id, "warning", f"member {info.filename} not read: {exc}"))
                    continue
                exact = stored and export.exact

                def span(a: int, b: int, start=start, member_span=member_span, exact=exact):
                    if not exact:
                        return member_span
                    return base.span(data, min(len(data), start + a), min(len(data), start + b))

                reader = Reader(path, blob, base, span, member_span, self.id)
                reader.run(export)
                items += reader.items
                diagnostics += reader.diagnostics
                resolved += reader.resolved
                unresolved += reader.unresolved
        from repo_intel.extractors import sql_lineage

        return base.Extraction(items + sql_lineage.lineage_summary(blob, data, resolved, unresolved), diagnostics)


def _member_range(data: bytes, info: zipfile.ZipInfo) -> tuple[int, int]:
    """The bytes of a member's (possibly compressed) data in the archive, from its local header."""
    h = info.header_offset
    header = data[h:h + 30]
    if len(header) < 30 or header[:4] != b"PK\x03\x04":
        return 0, len(data)
    name_len = int.from_bytes(header[26:28], "little")
    extra_len = int.from_bytes(header[28:30], "little")
    start = h + 30 + name_len + extra_len
    return start, min(len(data), start + info.compress_size)
