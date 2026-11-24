"""GI-007: manifests, workspaces, lockfiles and `unit.plan`."""

import json

from repo_intel.contracts import Evidence, Span
from repo_intel.extractors import MANIFEST_BASENAMES
from repo_intel.extractors.manifests import CargoToml, PackageJson, PackageLock, PnpmLock, PnpmWorkspace, PyProject, TomlLock, YarnLock
from repo_intel.extractors.tsconfig import TsConfig
from repo_intel.planning import glob_match, plan_units

BLOB = "ab" * 32


def _payloads(extractor, path, text, kind):
    data = text.encode() if isinstance(text, str) else text
    return [o.payload for o in extractor.extract(path, data, BLOB).observations if o.kind == kind]


def _summary(path, extractor, text):
    data = text.encode() if isinstance(text, str) else text
    return {"path": path, "observations": [o.model_dump(mode="json") for o in extractor.extract(path, data, BLOB).observations]}


def _pkg(path, name, deps=None, **extra):
    return _summary(path, PackageJson(), json.dumps({"name": name, "dependencies": deps or {}, **extra}))


# ---------------------------------------------------------------- workspaces


def test_workspace_globs_match_one_level_or_any_depth():
    assert glob_match("apps/*", "apps/web") and not glob_match("apps/*", "apps/web/nested")
    assert glob_match("packages/**", "packages/a/b") and glob_match("./tools/cli/", "tools/cli")
    assert glob_match("libs/*-kit", "libs/ui-kit") and not glob_match("libs/*-kit", "libs/ui")


def test_workspace_declarations_from_npm_yarn_pnpm_uv_and_cargo():
    assert _payloads(PackageJson(), "package.json", '{"name": "r", "workspaces": ["apps/*"]}', "workspace_declaration") == [{"ecosystem": "npm", "tool": "npm", "dir": "", "globs": ["apps/*"], "exclude": []}]
    assert _payloads(PackageJson(), "package.json", '{"name": "r", "workspaces": {"packages": ["libs/*"], "nohoist": ["x"]}}', "workspace_declaration")[0]["globs"] == ["libs/*"]
    pnpm = _payloads(PnpmWorkspace(), "pnpm-workspace.yaml", "packages:\n  - apps/*\n  - '!apps/legacy'\n", "workspace_declaration")
    assert pnpm == [{"ecosystem": "npm", "tool": "pnpm", "dir": "", "globs": ["apps/*"], "exclude": ["apps/legacy"]}]
    uv = _payloads(PyProject(), "pyproject.toml", '[tool.uv.workspace]\nmembers = ["services/*"]\nexclude = ["services/old"]\n', "workspace_declaration")
    assert uv == [{"ecosystem": "pypi", "tool": "uv", "dir": "", "globs": ["services/*"], "exclude": ["services/old"]}]
    cargo = _payloads(CargoToml(), "Cargo.toml", '[workspace]\nmembers = ["crates/*"]\n', "workspace_declaration")
    assert cargo == [{"ecosystem": "cargo", "tool": "cargo", "dir": "", "globs": ["crates/*"], "exclude": []}]


def test_cargo_toml_is_inventory_only():
    out = CargoToml().extract("tools/fmt/Cargo.toml", b'[package]\nname = "fmt"\nversion = "0.1.0"\n\n[dependencies]\nserde = { version = "1.0" }\nclap = "4"\nshared = { workspace = true }\n\n[dev-dependencies]\ninsta = "1"\n', BLOB)
    kinds = sorted({o.kind for o in out.observations})
    assert kinds == ["dependency_declaration", "package_declaration"]
    assert out.observations[0].payload["ecosystem"] == "cargo" and out.observations[0].payload["manifest_dir"] == "tools/fmt"
    deps = {o.payload["name"]: (o.payload["specifier"], o.payload["scope"]) for o in out.observations if o.kind == "dependency_declaration"}
    assert deps == {"clap": ("4", "runtime"), "serde": ("1.0", "runtime"), "shared": ("workspace", "runtime"), "insta": ("1", "dev")}
    assert CargoToml().extract("Cargo.toml", b"[package\n", BLOB).parsed is False


def test_manifest_basenames_cover_workspace_files_lockfiles_and_any_tsconfig():
    for name in ("package.json", "pnpm-workspace.yaml", "pnpm-lock.yaml", "package-lock.json", "yarn.lock", "poetry.lock", "uv.lock", "Cargo.toml", "Cargo.lock", "tsconfig.json", "tsconfig.base.json", "jsconfig.json"):
        assert name in MANIFEST_BASENAMES, name
    assert "index.ts" not in MANIFEST_BASENAMES and "settings.json" not in MANIFEST_BASENAMES


# ------------------------------------------------------------------ lockfiles


def test_package_lock_v3_resolves_each_importer_and_prefers_nested_installs():
    lock = {"lockfileVersion": 3, "packages": {
        "": {"name": "root", "workspaces": ["apps/*"], "devDependencies": {"typescript": "^5"}},
        "apps/api": {"name": "api", "dependencies": {"express": "^5", "shared": "*"}},
        "node_modules/typescript": {"version": "5.9.3", "resolved": "https://registry.example/typescript.tgz", "integrity": "sha512-x"},
        "node_modules/express": {"version": "4.21.0"},
        "apps/api/node_modules/express": {"version": "5.1.0"},
        "node_modules/shared": {"resolved": "packages/shared", "link": True},
    }}
    [p] = _payloads(PackageLock(), "package-lock.json", json.dumps(lock), "lockfile")
    assert p["importers"] == {".": {"typescript": "5.9.3"}, "apps/api": {"express": "5.1.0"}}
    assert "registry.example" not in json.dumps(p) and "sha512" not in json.dumps(p), "URLs and hashes are not copied"
    [v1] = _payloads(PackageLock(), "package-lock.json", '{"lockfileVersion": 1, "dependencies": {"lodash": {"version": "4.17.21"}}}', "lockfile")
    assert v1["importers"] == {".": {"lodash": "4.17.21"}}


def test_pnpm_lock_v5_and_v9_strip_peer_suffixes_and_skip_links():
    v9 = "lockfileVersion: '9.0'\nimporters:\n  .:\n    dependencies:\n      next:\n        specifier: ^16\n        version: 16.4.2(react@19.2.6)\n      shared:\n        specifier: workspace:*\n        version: link:../shared\n"
    assert _payloads(PnpmLock(), "pnpm-lock.yaml", v9, "lockfile")[0]["importers"] == {".": {"next": "16.4.2"}}
    v5 = "lockfileVersion: 5.4\nspecifiers:\n  react: ^18\ndependencies:\n  react: 18.3.1\n"
    assert _payloads(PnpmLock(), "pnpm-lock.yaml", v5, "lockfile")[0]["importers"] == {".": {"react": "18.3.1"}}


def test_yarn_classic_and_berry_keys_map_specs_to_versions():
    classic = '# yarn lockfile v1\n\n"express@^5.0.0", express@^5.1.0:\n  version "5.1.0"\n  resolved "https://registry.yarnpkg.com/express.tgz#abc"\n'
    assert _payloads(YarnLock(), "yarn.lock", classic, "lockfile")[0]["specs"] == {"express@^5.0.0": "5.1.0", "express@^5.1.0": "5.1.0"}
    berry = '__metadata:\n  version: 8\n\n"react@npm:^19":\n  version: 19.2.6\n  resolution: "react@npm:19.2.6"\n\n"web@workspace:apps/web":\n  version: 0.0.0-use.local\n'
    assert _payloads(YarnLock(), "yarn.lock", berry, "lockfile")[0]["specs"] == {"react@npm:^19": "19.2.6"}


def test_poetry_uv_and_cargo_locks():
    poetry = '[[package]]\nname = "FastAPI"\nversion = "0.115.0"\n\n[[package]]\nname = "pydantic_core"\nversion = "2.27.1"\n'
    assert _payloads(TomlLock("poetry.lock"), "poetry.lock", poetry, "lockfile")[0]["versions"] == {"fastapi": "0.115.0", "pydantic-core": "2.27.1"}
    uv = 'version = 1\n[[package]]\nname = "svc"\nversion = "0.1.0"\nsource = { editable = "." }\n\n[[package]]\nname = "httpx"\nversion = "0.28.1"\nsource = { registry = "https://pypi.org/simple" }\n'
    assert _payloads(TomlLock("uv.lock"), "uv.lock", uv, "lockfile")[0]["versions"] == {"httpx": "0.28.1"}
    cargo = 'version = 4\n[[package]]\nname = "me"\nversion = "0.1.0"\n\n[[package]]\nname = "serde"\nversion = "1.0.214"\nsource = "registry+https://github.com/rust-lang/crates.io-index"\n'
    assert _payloads(TomlLock("Cargo.lock"), "Cargo.lock", cargo, "lockfile")[0]["versions"] == {"serde": "1.0.214"}


# ----------------------------------------------------------------------- plan


def test_plan_expands_workspace_globs_and_only_members_are_siblings():
    summaries = [
        _summary("pnpm-workspace.yaml", PnpmWorkspace(), "packages:\n  - apps/*\n  - packages/*\n  - '!packages/scratch'\n"),
        _pkg("package.json", "root"),
        _pkg("apps/web/package.json", "web", {"ui": "workspace:*", "legacy": "^1"}),
        _pkg("packages/ui/package.json", "ui"),
        _pkg("packages/scratch/package.json", "scratch"),
        _pkg("examples/legacy/package.json", "legacy"),
    ]
    paths = sorted(s["path"] for s in summaries) + ["apps/web/src/a.ts", "scripts/x.sh"]
    plan = plan_units(paths, summaries)
    units = {u["name"]: u for u in plan["units"]}
    assert set(units) == {".", "apps/web", "packages/ui", "packages/scratch", "examples/legacy"}
    assert units["apps/web"]["workspace"] == "" and units["packages/ui"]["workspace"] == ""
    assert "workspace" not in units["packages/scratch"] and "workspace" not in units["examples/legacy"]
    assert [p["name"] for p in plan["packages"]] == ["root", "web", "ui"]
    assert sorted(p["name"] for p in plan["non_member_packages"]) == ["legacy", "scratch"]
    assert units["."]["paths"] == ["package.json", "pnpm-workspace.yaml", "scripts/x.sh"]


def test_plan_without_a_workspace_keeps_every_package_a_sibling():
    plan = plan_units(["a/package.json", "b/package.json"], [_pkg("a/package.json", "a"), _pkg("b/package.json", "b")])
    assert [p["name"] for p in plan["packages"]] == ["a", "b"] and "non_member_packages" not in plan


def test_plan_matches_lockfile_versions_to_declared_dependencies_per_importer():
    lock = "lockfileVersion: '9.0'\nimporters:\n  .: {}\n  apps/api:\n    dependencies:\n      express:\n        specifier: 5.1.0\n        version: 5.1.0\n      ui:\n        specifier: workspace:*\n        version: link:../../packages/ui\n"
    summaries = [
        _summary("pnpm-workspace.yaml", PnpmWorkspace(), "packages: ['apps/*', 'packages/*']\n"),
        _pkg("package.json", "root"),
        _pkg("apps/api/package.json", "api", {"express": "5.1.0", "ui": "workspace:*", "left-pad": "1"}),
        _pkg("packages/ui/package.json", "ui"),
        _summary("pnpm-lock.yaml", PnpmLock(), lock),
    ]
    plan = plan_units(sorted(s["path"] for s in summaries), summaries)
    [resolved] = next(u for u in plan["units"] if u["name"] == "apps/api")["resolved"]
    assert resolved["versions"] == {"express": "5.1.0"}, "siblings and unlocked dependencies are left out"
    assert (resolved["lockfile"], resolved["tool"], resolved["manifest"]) == ("pnpm-lock.yaml", "pnpm", "apps/api/package.json")
    assert len(resolved["evidence"]) == 2


def test_plan_follows_tsconfig_extends_across_directories_and_packages():
    summaries = [
        _pkg("package.json", "root", workspaces=["apps/*", "tooling/*"]),
        _pkg("tooling/tsconfig/package.json", "@acme/tsconfig"),
        _summary("tooling/tsconfig/tsconfig.strict.json", TsConfig(), '{"compilerOptions": {"strict": true, "baseUrl": "."}}'),
        _summary("tsconfig.base.json", TsConfig(), '{"compilerOptions": {"baseUrl": ".", "paths": {"@shared/*": ["libs/shared/*"]}}}'),
        _pkg("apps/web/package.json", "web"),
        _summary("apps/web/tsconfig.json", TsConfig(), '{"extends": ["@acme/tsconfig/tsconfig.strict.json", "../../tsconfig.base.json"]}'),
        _pkg("apps/admin/package.json", "admin"),
        _summary("apps/admin/tsconfig.json", TsConfig(), '{"extends": "../../tsconfig.base", "compilerOptions": {"paths": {"@/*": ["./src/*"]}}}'),
        _pkg("apps/cli/package.json", "cli"),
        _summary("apps/cli/tsconfig.json", TsConfig(), '{"extends": "@tsconfig/node22/tsconfig.json"}'),
    ]
    plan = plan_units(sorted(s["path"] for s in summaries), summaries)
    units = {u["name"]: u for u in plan["units"]}
    [web] = units["apps/web"]["ts_configs"]
    assert (web["root"], web["paths"]) == ("", {"@shared/*": ["libs/shared/*"]}), "later bases win; paths inherited; baseUrl stays relative to the config that set it"
    [admin] = units["apps/admin"]["ts_configs"]
    assert (admin["root"], admin["paths"]) == ("", {"@/*": ["./src/*"]}), "own paths replace inherited ones; inherited baseUrl still applies"
    assert "ts_configs" not in units["apps/cli"], "an external base adds no aliases"


def test_plan_records_package_entry_points_and_maps_dist_to_src():
    summaries = [
        _pkg("packages/ui/package.json", "@acme/ui", exports={".": {"types": "./dist/index.d.ts", "import": "./dist/index.js"}, "./button": "./src/button.tsx"}),
        _pkg("packages/shared/package.json", "@acme/shared", main="src/index.ts"),
    ]
    paths = ["packages/ui/package.json", "packages/ui/src/index.ts", "packages/ui/src/button.tsx", "packages/shared/package.json", "packages/shared/src/index.ts"]
    plan = plan_units(paths, summaries)
    ui = next(p for p in plan["packages"] if p["name"] == "@acme/ui")
    assert ui["entries"] == {".": "packages/ui/src/index.ts", "./button": "packages/ui/src/button.tsx"} and ui["exports_strict"] is True
    shared = next(p for p in plan["packages"] if p["name"] == "@acme/shared")
    assert shared["entries"] == {".": "packages/shared/src/index.ts"} and shared["exports_strict"] is False


# ------------------------------------------------------------- compiled fixture


def test_monorepo_lockfile_versions_and_workspace_boundary(tmp_path):
    from conftest import REPO_ID
    from repo_intel.contracts import entity_id
    from repo_intel.fixtures import materialize
    from repo_intel.pipeline import compile_commit, load_shards
    from repo_intel.store import ObjectStore

    shas = materialize("F02-ts-monorepo", tmp_path / "repo")
    store = ObjectStore(tmp_path / "store", "t")
    for sha in shas:
        manifest = compile_commit(store, tmp_path / "repo", REPO_ID, sha).manifest
    shards = load_shards(store, manifest)
    entities = {e["id"]: e for s in shards.values() for e in s["entities"]}
    edges = [a for s in shards.values() for a in s["assertions"]]
    api = entities[entity_id(REPO_ID, "Package", {"ecosystem": "npm", "name": "@acme/api", "manifest_dir": "apps/api"})]
    assert api["attributes"]["resolved_dependencies"] == {"@prisma/client": "6.9.0", "express": "5.1.0"}
    worker = entity_id(REPO_ID, "Package", {"ecosystem": "npm", "name": "@acme/worker", "manifest_dir": "apps/worker"})
    legacy_pkg = entity_id(REPO_ID, "Package", {"ecosystem": "npm", "name": "@acme/legacy", "manifest_dir": "tools/legacy"})
    legacy_dep = entity_id(REPO_ID, "ExternalDependency", {"ecosystem": "npm", "name": "@acme/legacy"})
    depends = {a["object"] for a in edges if a["subject"] == worker and a["predicate"] == "DEPENDS_ON"}
    assert legacy_dep in depends and legacy_pkg not in depends, "a package outside the workspace globs is not a sibling"
    assert legacy_pkg in entities
