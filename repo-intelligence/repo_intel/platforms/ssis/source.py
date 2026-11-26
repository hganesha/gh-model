"""The SSIS pack's extractors: packages, project files and project deployment archives.

- `ssis.package` reads a `.dtsx` package (repo_intel.platforms.ssis.package).
- `ssis.project` reads a project connection manager (`.conmgr`) as a ConfigKey and, for a
  flat file, its record layout; and `Project.params` (any `.params` file whose root is
  `SSIS:Parameters`) as one ConfigKey per parameter,
  `parameter:<project directory>#$Project::<name>`. Values, connection strings and
  sensitive parameters' contents are never read.
- `ssis.ispac` reads a project deployment file (`.ispac`, a zip archive) from its bytes:
  `@Project.manifest` (which packages are entry points), and each package, connection
  manager and `Project.params` inside it as above, named `<archive path>#<member>`.

A package's project is its directory: an Execute Package task with a project reference
runs the package of that name beside it (or in the same archive).
"""

from __future__ import annotations

import posixpath
import re
from urllib.parse import unquote

from repo_intel.platforms.ssis.emit import Out
from repo_intel.platforms.ssis.package import PackageReader, declare_connection, is_true, prop
from repo_intel.platforms.ssis.xmlread import Doc, Place, ZipReader, attr, child, children, head_text, local, parse

_NAME_OK = re.compile(r"[^\x00-\x1f<>]{1,200}")


def _project_dir(path: str) -> str:
    return posixpath.dirname(path) or "."


def _is_package(doc: Doc) -> bool:
    root = doc.root
    return local(root.tag) == "Executable" and "package" in (prop(root, "ExecutableType") or prop(root, "CreationName") or "").lower()


def read_package(out: Out, data: bytes, place: Place, job_name: str, project: str, sibling, extra=None, project_connections=None) -> bool:
    doc, problem = parse(data)
    if doc is None:
        out.note("warning", f"{job_name}: {problem}")
        return False
    if not _is_package(doc):
        out.note("info", f"{job_name}: not an SSIS package (no package executable at the root)")
        return False
    PackageReader(out, doc, data, place, job_name, project, sibling, extra, project_connections).run()
    return True


def read_connection(out: Out, data: bytes, place: Place, label: str, known: dict[str, str] | None = None) -> bool:
    """A project connection manager; `known` collects its DTSID -> name for the packages beside it."""
    doc, problem = parse(data)
    if doc is None:
        out.note("warning", f"{label}: {problem}")
        return False
    if local(doc.root.tag) != "ConnectionManager":
        return False

    def at(el):
        s = doc.span(el)
        return out.span(place, *s) if s else out.span(place, 0, len(data))

    name = declare_connection(out, at(doc.root), doc.root, "project", at)
    out.construct(at(doc.root), "ssis:project_connection_manager", name is not None)
    dtsid = prop(doc.root, "DTSID")
    if name and dtsid and known is not None:
        known[dtsid] = name
    return True


def read_parameters(out: Out, data: bytes, place: Place, project: str, label: str) -> bool:
    doc, problem = parse(data)
    if doc is None:
        out.note("warning", f"{label}: {problem}")
        return False
    if local(doc.root.tag) != "Parameters":
        return False
    for p in children(doc.root, "Parameter"):
        s = doc.span(p)
        at = out.span(place, *s) if s else out.span(place, 0, len(data))
        name = attr(p, "Name")
        ok = bool(name and _NAME_OK.fullmatch(name))
        out.construct(at, "ssis:project_parameter", ok)
        if not ok:
            continue
        props = {attr(x, "Name"): (x.text or "").strip() for x in children(child(p, "Properties"), "Property")}
        key = f"parameter:{project}#$Project::{name}"
        dtype = props.get("DataType")
        out.entity(at, "ssis:Parameter", {"name": key}, key, {
            "scope": "project", "data_type": dtype if dtype and re.fullmatch(r"\d{1,3}", dtype) else None,
            "required": True if is_true(props.get("Required")) else None, "sensitive": True if is_true(props.get("Sensitive")) else None,
        })
    return True


class SsisPackageSource:
    id = "ssis.package"
    version = "1.0.0"

    def matches(self, path: str) -> bool:
        return path.lower().endswith(".dtsx")

    def extract(self, path: str, data: bytes, blob: str):
        from repo_intel.extractors import base

        out = Out(self.id, path, data, blob, base)
        here = posixpath.dirname(path)
        ok = read_package(out, data, Place(0, len(data), True), path, _project_dir(path), lambda n: posixpath.join(here, n) if here else n)
        return out.extraction(parsed=ok)


class SsisProjectFiles:
    id = "ssis.project"
    version = "1.0.0"

    def matches(self, path: str) -> bool:
        return path.lower().endswith((".conmgr", ".params"))

    def extract(self, path: str, data: bytes, blob: str):
        from repo_intel.extractors import base

        out = Out(self.id, path, data, blob, base)
        place = Place(0, len(data), True)
        if path.lower().endswith(".conmgr"):
            ok = read_connection(out, data, place, path)
        else:
            if "Parameters" not in head_text(data):
                return base.Extraction(parsed=False)
            ok = read_parameters(out, data, place, _project_dir(path), path)
        return out.extraction(parsed=ok)


class SsisProjectArchive:
    id = "ssis.ispac"
    version = "1.0.0"

    def matches(self, path: str) -> bool:
        return path.lower().endswith(".ispac")

    def extract(self, path: str, data: bytes, blob: str):
        from repo_intel.extractors import base

        out = Out(self.id, path, data, blob, base)
        z = ZipReader(data)
        problem = z.open()
        if problem:
            return base.Extraction(diagnostics=[base.diagnostic(self.id, "warning" if "zip" in problem else "info", problem)], parsed=False)
        try:
            names = z.names()
            entry_points: dict[str, bool] = {}
            manifest = next((n for n in names if n.lower() == "@project.manifest"), None)
            if manifest:
                m = z.read(manifest)
                if m is not None:
                    doc, why = parse(m.data)
                    if doc is not None:
                        s = doc.span(doc.root)
                        out.construct(out.span(m.place, *s) if s else out.span(m.place, 0, len(m.data)), "ssis:project_manifest", True)
                        for pk in (x for x in doc.root.iter() if local(x.tag) == "Package"):
                            pname = attr(pk, "Name")
                            if pname:
                                entry_points[pname] = is_true(attr(pk, "EntryPoint"))
                    else:
                        out.note("warning", f"@Project.manifest: {why}")
            # Connection managers and parameters first, so packages can name project connections by id.
            known: dict[str, str] = {}
            for name in sorted(names, key=lambda n: (n.lower().endswith(".dtsx"), n)):
                low = name.lower()
                label = unquote(name)
                if not low.endswith((".dtsx", ".conmgr", ".params")):
                    continue
                member = z.read(name)
                if member is None:
                    continue
                if low.endswith(".dtsx"):
                    extra = {"archive": path, "entry_point": True if entry_points.get(label) else None}
                    read_package(out, member.data, member.place, f"{path}#{label}", path, lambda n: f"{path}#{n}", extra, known)
                elif low.endswith(".conmgr"):
                    read_connection(out, member.data, member.place, f"{path}#{label}", known)
                else:
                    read_parameters(out, member.data, member.place, path, f"{path}#{label}")
            for severity, message in z.notes:
                out.note(severity, message)
        finally:
            z.close()
        return out.extraction()
