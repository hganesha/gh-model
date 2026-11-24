"""`unit.plan`: partition a repository into compilation units (spec §3.3, GI-007).

Works from the manifest summaries only (package manifests, workspace files,
lockfiles and tsconfig files) plus the list of paths. Everything that crosses
a unit boundary and that a single file cannot know is settled here:

  units       one per package directory; every path belongs to the deepest
              package that contains it, or to "@root"
  packages    the packages that are workspace siblings of each other: when a
              workspace is declared for an ecosystem, only its members (and its
              root) are; a stray package.json under `examples/` is not
  workspace   on a unit: the directory of the workspace it is a member of
  resolved    on a unit: for each package, the lockfile versions of its
              declared dependencies
  ts_configs  on a unit: the effective tsconfig path aliases that apply to it,
              with `extends` chains followed across directories
  entries     on an npm package: public subpath -> file, from `exports`,
              `types` or `main`, for binding workspace imports to files
"""

from __future__ import annotations

import posixpath
from collections.abc import Iterable, Mapping
from typing import Any

from repo_intel.extractors.manifests import effective_declarations

ROOT_UNIT = "@root"
TS_EXT = (".ts", ".tsx", ".d.ts", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts")
_CONFIG_PREFERENCE = ("tsconfig.json", "jsconfig.json")


def unit_name(manifest_dir: str) -> str:
    return manifest_dir or "."


def _of(summaries: Iterable[Mapping[str, Any]], kind: str) -> list[tuple[str, dict[str, Any]]]:
    return [(s["path"], o) for s in summaries for o in s["observations"] if o["kind"] == kind]


def _norm(path: str) -> str:
    path = posixpath.normpath(path) if path else ""
    return "" if path in (".", "") else path


def _within(path: str, root: str) -> bool:
    return root == "" or path == root or path.startswith(root + "/")


def _rel(path: str, root: str) -> str:
    return path if root == "" else path[len(root) + 1:] if path != root else ""


# ------------------------------------------------------------------ workspaces


def _segment_match(pattern: str, segment: str) -> bool:
    import fnmatch
    return fnmatch.fnmatchcase(segment, pattern)


def glob_match(pattern: str, rel: str) -> bool:
    """Workspace glob semantics: `*` matches within one directory level, `**` any depth."""
    pattern = pattern.strip().removeprefix("./").rstrip("/")
    want = [p for p in pattern.split("/") if p not in ("", ".")]
    have = [p for p in rel.split("/") if p] if rel else []

    def go(i: int, j: int) -> bool:
        if i == len(want):
            return j == len(have)
        if want[i] == "**":
            return any(go(i + 1, k) for k in range(j, len(have) + 1))
        return j < len(have) and _segment_match(want[i], have[j]) and go(i + 1, j + 1)

    return go(0, 0)


def _workspaces(summaries: list[Mapping[str, Any]], packages: list[dict[str, Any]]) -> tuple[dict[str, str], set[str]]:
    """(manifest_dir -> workspace dir for each member, ecosystems that declare a workspace)."""
    member_of: dict[str, str] = {}
    declared: set[str] = set()
    for _, o in sorted(_of(summaries, "workspace_declaration"), key=lambda x: x[0]):
        p = o["payload"]
        declared.add(p["ecosystem"])
        for pkg in packages:
            if pkg["ecosystem"] != p["ecosystem"] or not _within(pkg["manifest_dir"], p["dir"]) or pkg["manifest_dir"] == p["dir"]:
                continue
            rel = _rel(pkg["manifest_dir"], p["dir"])
            if any(glob_match(g, rel) for g in p["globs"]) and not any(glob_match(g, rel) for g in p.get("exclude", [])):
                member_of.setdefault(pkg["manifest_dir"], p["dir"])
    return member_of, declared


# ------------------------------------------------------------------- lockfiles


def _resolved(summaries: list[Mapping[str, Any]], packages: list[dict[str, Any]], siblings: set[tuple[str, str]]) -> dict[str, list[dict[str, Any]]]:
    """unit name -> [{package, lockfile, versions}] for each package with a lockfile above it."""
    locks = sorted(_of(summaries, "lockfile"), key=lambda x: (-len(x[1]["payload"]["dir"]), x[0]))
    declarations: dict[str, list[dict[str, Any]]] = {}
    manifest_of: dict[tuple[str, str, str], tuple[str, dict[str, Any]]] = {}
    for s in summaries:
        own = [o for o in s["observations"] if o["kind"] == "package_declaration"]
        for o in own:
            p = o["payload"]
            manifest_of[(p["ecosystem"], p["name"], p["manifest_dir"])] = (s["path"], o["evidence"])
            declarations[f"{p['ecosystem']}:{p['name']}:{p['manifest_dir']}"] = [d["payload"] for d in s["observations"] if d["kind"] == "dependency_declaration" and d["payload"]["ecosystem"] == p["ecosystem"]]
    out: dict[str, list[dict[str, Any]]] = {}
    for pkg in packages:
        lock = next(((path, o) for path, o in locks if o["payload"]["ecosystem"] == pkg["ecosystem"] and _within(pkg["manifest_dir"], o["payload"]["dir"])), None)
        found = (pkg["ecosystem"], pkg["name"], pkg["manifest_dir"]) in manifest_of
        if lock is None or not found:
            continue
        path, o = lock
        lp = o["payload"]
        importer = _rel(pkg["manifest_dir"], lp["dir"]) or "."
        versions: dict[str, str] = {}
        for dep in declarations[f"{pkg['ecosystem']}:{pkg['name']}:{pkg['manifest_dir']}"]:
            if (dep["ecosystem"], dep["name"]) in siblings:
                continue
            if "importers" in lp:
                version = lp["importers"].get(importer, {}).get(dep["name"])
            elif "specs" in lp:
                version = lp["specs"].get(f"{dep['name']}@{dep['specifier']}") or lp["specs"].get(f"{dep['name']}@npm:{dep['specifier']}")
            else:
                version = lp["versions"].get(dep["name"])
            if version:
                versions[dep["name"]] = version
        if versions:
            manifest, manifest_ev = manifest_of[(pkg["ecosystem"], pkg["name"], pkg["manifest_dir"])]
            out.setdefault(unit_name(pkg["manifest_dir"]), []).append({
                "package": {k: pkg[k] for k in ("ecosystem", "name", "manifest_dir")}, "manifest": manifest,
                "lockfile": path, "tool": lp["tool"], "versions": dict(sorted(versions.items())),
                "evidence": [manifest_ev, o["evidence"]],
            })
    return out


# -------------------------------------------------------------------- tsconfig


def probe(stem: str, files: set[str] | frozenset[str]) -> str | None:
    """The file a TypeScript import of `stem` lands on, by extension and index probing."""
    stem = _norm(stem)
    candidates = [stem] + [stem + e for e in TS_EXT] + [posixpath.join(stem, "index" + e) for e in TS_EXT]
    base, ext = posixpath.splitext(stem)
    if ext in (".js", ".jsx", ".mjs", ".cjs"):  # `./x.js` written for a file that is x.ts
        candidates += [base + e for e in (".ts", ".tsx", ".mts", ".cts")]
    return next((c for c in candidates if c in files), None)


def _ts_configs(summaries: list[Mapping[str, Any]], packages: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """config path -> effective {dir, root, paths, evidence, unresolved_extends}, following `extends`."""
    raw = {path: o for path, o in _of(summaries, "ts_config")}
    npm_dirs = {p["name"]: p["manifest_dir"] for p in packages if p["ecosystem"] == "npm"}
    memo: dict[str, dict[str, Any]] = {}

    def target(spec: str, here: str) -> str | None:
        if spec.startswith((".", "/")):
            stem = _norm(posixpath.join(here, spec.lstrip("/") if spec.startswith("/") else spec))
        else:  # a package: only a sibling workspace package can be followed
            parts = spec.split("/")
            name, rest = ("/".join(parts[:2]), parts[2:]) if spec.startswith("@") else (parts[0], parts[1:])
            if name not in npm_dirs:
                return None
            stem = _norm(posixpath.join(npm_dirs[name], *rest)) if rest else posixpath.join(npm_dirs[name], "tsconfig.json")
        return next((c for c in (stem, stem + ".json", posixpath.join(stem, "tsconfig.json")) if c in raw), None)

    def effective(path: str, seen: tuple[str, ...]) -> dict[str, Any]:
        if path in memo:
            return memo[path]
        p = raw[path]["payload"]
        here = p["dir"]
        out: dict[str, Any] = {"base_dir": None, "paths": None, "paths_dir": None, "evidence": [], "unresolved_extends": []}
        for spec in p.get("extends", []):
            parent = target(spec, here)
            if parent is None or parent in seen:
                out["unresolved_extends"].append(spec)
                continue
            inherited = effective(parent, (*seen, parent))
            for k in ("base_dir", "paths", "paths_dir"):
                if inherited[k] is not None:
                    out[k] = inherited[k]
            out["evidence"] = out["evidence"] + [e for e in inherited["evidence"] if e not in out["evidence"]]
            out["unresolved_extends"] += inherited["unresolved_extends"]
        if p.get("base_url") is not None:
            out["base_dir"] = _norm(posixpath.join(here, p["base_url"]))
        if p.get("paths") is not None:
            out["paths"], out["paths_dir"] = p["paths"], here
        out["evidence"] = out["evidence"] + [raw[path]["evidence"]]
        memo[path] = out
        return out

    result = {}
    for path in sorted(raw):
        e = effective(path, (path,))
        if e["paths"]:
            root = e["base_dir"] if e["base_dir"] is not None else e["paths_dir"]
            result[path] = {"config": path, "dir": raw[path]["payload"]["dir"], "root": root, "paths": e["paths"], "evidence": e["evidence"], "unresolved_extends": sorted(set(e["unresolved_extends"]))}
    return result


def _configs_for(unit_root: str, unit_paths: list[str], effective: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """The configs in the unit, and the nearest tsconfig.json above it when its root has none."""
    own = [effective[p] for p in unit_paths if p in effective]
    if not any(c["dir"] == unit_root for c in own) and unit_root:
        here = unit_root
        while here:
            here = posixpath.dirname(here)
            above = [effective[c] for c in (posixpath.join(here, n) if here else n for n in _CONFIG_PREFERENCE) if c in effective]
            if above:
                own.append(above[0])
                break
    return sorted(own, key=lambda c: (c["dir"], c["config"]))


def pick_config(path: str, configs: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The config TypeScript would use for `path`: the deepest enclosing directory's,
    tsconfig.json over jsconfig.json over the others."""
    enclosing = [c for c in configs if _within(posixpath.dirname(path), c["dir"])]
    if not enclosing:
        return None
    deepest = max(len(c["dir"]) for c in enclosing)
    rank = lambda c: (_CONFIG_PREFERENCE.index(posixpath.basename(c["config"])) if posixpath.basename(c["config"]) in _CONFIG_PREFERENCE else len(_CONFIG_PREFERENCE), c["config"])
    return min((c for c in enclosing if len(c["dir"]) == deepest), key=rank)


# ---------------------------------------------------------------- entry points


def _entries(summaries: list[Mapping[str, Any]], files: frozenset[str]) -> dict[tuple[str, str], dict[str, Any]]:
    """(name, manifest_dir) -> {"entries": {subpath: file or pattern}, "strict": bool}.

    A target that does not exist in the repository but sits under dist/, build/ or
    lib/ is mapped to the same path under src/ (a TypeScript source package that
    publishes compiled output); such entries are recorded under "mapped"."""
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for _, o in _of(summaries, "package_entry"):
        p = o["payload"]
        entries, mapped = {}, []
        for subpath, raw_target in sorted(p["entries"].items()):
            target = _norm(posixpath.join(p["manifest_dir"], raw_target))
            if "*" in subpath or "*" in target:
                entries[subpath] = target
                continue
            found = probe(target, files)
            if found is None:
                rel = _rel(target, p["manifest_dir"])
                head, _, rest = rel.partition("/")
                if head in ("dist", "build", "lib", "out") and rest:
                    stem = posixpath.join(p["manifest_dir"], "src", rest)
                    stem = stem.removesuffix(".d.ts") if stem.endswith(".d.ts") else posixpath.splitext(stem)[0]
                    found = probe(stem, files)
                    if found:
                        mapped.append(subpath)
            if found:
                entries[subpath] = found
        # With an `exports` map, a subpath it does not list cannot be imported.
        out[(p["name"], p["manifest_dir"])] = {"entries": entries, "strict": bool(p.get("exports")), "mapped": mapped}
    return out


# ------------------------------------------------------------------------ plan


def plan_units(paths: list[str], manifest_summaries: list[Mapping[str, Any]]) -> dict[str, Any]:
    files = frozenset(paths)
    packages = sorted(
        ({k: o["payload"][k] for k in ("ecosystem", "name", "manifest_dir")} for _, o in effective_declarations(_of(manifest_summaries, "package_declaration"))[0]),
        key=lambda p: (p["manifest_dir"], p["ecosystem"], p["name"]),
    )
    roots = sorted({p["manifest_dir"] for p in packages})

    def owner(path: str) -> str:
        best: str | None = None
        for root in roots:
            if _within(path, root) and (best is None or len(root) > len(best)):
                best = root
        return ROOT_UNIT if best is None else unit_name(best)

    members: dict[str, list[str]] = {unit_name(r): [] for r in roots}
    for path in sorted(paths):
        members.setdefault(owner(path), []).append(path)

    member_of, declared = _workspaces(manifest_summaries, packages)
    workspace_dirs = {o["payload"]["dir"] for _, o in _of(manifest_summaries, "workspace_declaration")}
    # With a workspace declared for an ecosystem, only its root and members are siblings.
    siblings = [p for p in packages if p["ecosystem"] not in declared or p["manifest_dir"] in member_of or p["manifest_dir"] in workspace_dirs]
    sibling_keys = {(p["ecosystem"], p["name"]) for p in siblings}
    entries = _entries(manifest_summaries, files)
    for p in siblings:
        found = entries.get((p["name"], p["manifest_dir"])) if p["ecosystem"] == "npm" else None
        if found and (found["entries"] or found["strict"]):
            p.update({"entries": found["entries"], "exports_strict": found["strict"]})

    resolved = _resolved(manifest_summaries, packages, sibling_keys)
    effective = _ts_configs(manifest_summaries, packages)
    units = []
    for name in sorted(members):
        unit: dict[str, Any] = {"name": name, "paths": members[name]}
        root = "" if name in (".", ROOT_UNIT) else name
        if name != ROOT_UNIT and root in member_of:
            unit["workspace"] = member_of[root]
        if name in resolved:
            unit["resolved"] = resolved[name]
        configs = _configs_for(root, members[name], effective)
        if configs:
            unit["ts_configs"] = configs
        units.append(unit)
    excluded = [p for p in packages if p not in siblings]
    return {"units": units, "packages": siblings, **({"non_member_packages": excluded} if excluded else {})}
