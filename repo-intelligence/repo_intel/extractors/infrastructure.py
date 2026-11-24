"""Infrastructure declarations: Dockerfile, GitHub Actions, Kubernetes, compose, Terraform.

Everything here is read from declarative text. Nothing is executed and no
configuration VALUE is copied into a payload: environment variables are
recorded by name only, Terraform resource arguments are not recorded at all.
Image references and base images are names, not secrets, and are kept.

Builds: a compose service with `build:` is BUILT_BY the Dockerfile it names;
a workflow's `docker build` / `docker/build-push-action` steps are recorded on
the Pipeline as `image_builds` (image repository, Dockerfile, context), which
repo-level linking matches against the images deployment units run.

Aliases: an environment variable whose whole value is another variable's name
(`DATABASE_URL: ${DB_URL}` in compose, `DB: ${{ secrets.PROD_DB }}` in a
workflow) is ALIAS_OF that variable. Only the two names are kept; a default
after `:-` is a value and is dropped.
"""

from __future__ import annotations

import posixpath
import re
from typing import Any

import yaml

from ..contracts import Span
from ..ontology import EntityKind, EvidenceClass, Predicate
from .base import Extraction, diagnostic, entity, obs, ref, relation, span, whole_file

E1, E4 = EvidenceClass.E1, EvidenceClass.E4
K = EntityKind

YAML_SUFFIXES = (".yaml", ".yml")
COMPOSE_BASENAMES = {"docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"}
_WORKFLOW = re.compile(r"(?:^|/)\.github/workflows/[^/]+\.ya?ml$")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")


def is_workflow(path: str) -> bool:
    return bool(_WORKFLOW.search(path))


def is_compose(path: str) -> bool:
    return posixpath.basename(path) in COMPOSE_BASENAMES


def load_yaml_documents(data: bytes) -> list[Any]:
    """Every YAML document in the file. Raises yaml.YAMLError / UnicodeDecodeError / RecursionError."""
    return list(yaml.safe_load_all(data.decode("utf-8")))


YAML_ERRORS = (yaml.YAMLError, UnicodeDecodeError, RecursionError, ValueError)


def _unparseable(extractor: str, what: str, exc: Exception) -> Extraction:
    # Only the error type and position: a parser message can quote file content.
    mark = getattr(exc, "problem_mark", None)
    where = f" at line {mark.line + 1}" if mark is not None else ""
    return Extraction(diagnostics=[diagnostic(extractor, "warning", f"unparseable {what}: {type(exc).__name__}{where}")], parsed=False)


def search_span(data: bytes, pattern: str, start: int = 0, end: int | None = None, group: int = 0) -> Span | None:
    """Span of the first match of a (multiline) regex inside data[start:end]."""
    m = re.compile(pattern.encode(), re.M).search(data, start, len(data) if end is None else end)
    return span(data, m.start(group), m.end(group)) if m else None


def yaml_key_span(data: bytes, key: str, value: Any = None, start: int = 0, end: int | None = None) -> Span | None:
    """Span of `key: value` (or `- key: value`) on one line; without a value, of `key:`."""
    k = rf"""["']?{re.escape(key)}["']?\s*:"""
    if value is None:
        return search_span(data, rf"^[ \t-]*({k})", start, end, 1)
    return search_span(data, rf"""^[ \t-]*({k}[ \t]*["']?{re.escape(str(value))}["']?)[ \t]*(?:#.*)?\r?$""", start, end, 1)


_COMPOSE_VAR = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)(?:[:?]?[-+?][^}]*)?\}$|^\$([A-Za-z_][A-Za-z0-9_]*)$")
_ACTIONS_VAR = re.compile(r"^\$\{\{\s*(secrets|vars|env)\.([A-Za-z_][A-Za-z0-9_]*)\s*\}\}$")


def image_repository(image: str) -> str:
    """registry/name of an image reference, without tag or digest: what two declarations share."""
    name = image.split("@", 1)[0]
    head, _, tail = name.rpartition("/")
    return f"{head}/{tail.split(':', 1)[0]}" if head else tail.split(":", 1)[0]


def _alias(blob: str, at: Span, name: str, target: str, basis: str) -> list:
    if not (_ENV_NAME.match(name) and target != name):
        return []
    key = ref(K.CONFIG_KEY, {"name": name}, name)
    other = ref(K.CONFIG_KEY, {"name": target}, target)
    return [entity(blob, at, K.CONFIG_KEY, {"name": target}, target, {}, E1), relation(blob, at, key, Predicate.ALIAS_OF, other, {"basis": basis}, E1)]


def _strs(values: Any) -> list[str]:
    return [v for v in values if isinstance(v, str)] if isinstance(values, list) else []


# --------------------------------------------------------------------------- Dockerfile

_INSTRUCTION = re.compile(rb"^[ \t]*([A-Za-z]+)(?:[ \t]+(.*?))?[ \t]*\r?$", re.M)


class Dockerfile:
    id = "infra.dockerfile"
    version = "1.0.0"

    def matches(self, path: str) -> bool:
        base = posixpath.basename(path)
        return base == "Dockerfile" or base.startswith("Dockerfile.") or base.endswith(".Dockerfile")

    def extract(self, path, data, blob):
        stages, bases, aliases, entrypoint, first_from = 0, [], set(), False, None
        for m in _INSTRUCTION.finditer(data):
            word = m.group(1).upper()
            if word in (b"ENTRYPOINT", b"CMD"):
                entrypoint = True
            elif word == b"FROM" and m.group(2):
                tokens = [t for t in m.group(2).decode("utf-8", "replace").split() if not t.startswith("--")]
                if not tokens:
                    continue
                stages += 1
                first_from = first_from or span(data, m.start(1), m.end())
                if tokens[0] not in aliases and tokens[0] not in bases:  # `FROM build` names an earlier stage, not an image
                    bases.append(tokens[0])
                if len(tokens) >= 3 and tokens[1].upper() == "AS":
                    aliases.add(tokens[2])
        if not stages:
            return Extraction(diagnostics=[diagnostic(self.id, "warning", "no FROM instruction found")], parsed=False)
        return Extraction([entity(blob, first_from, K.BUILD_TARGET, {"path": path}, path, {"base_images": bases, "has_entrypoint": entrypoint, "stages": stages}, E1)])


# --------------------------------------------------------------------------- GitHub Actions


_DOCKER_BUILD = re.compile(r"\bdocker\s+(?:image\s+)?(?:buildx\s+)?build\b([^&|;\n]*)")


def _docker_build_args(args: str) -> dict[str, str] | None:
    """-t/--tag image, -f/--file Dockerfile and the context of one `docker build` command."""
    tokens, out, i = args.split(), {}, 0
    positional = []
    while i < len(tokens):
        t = tokens[i]
        if t in ("-t", "--tag", "-f", "--file") and i + 1 < len(tokens):
            out.setdefault("image" if t in ("-t", "--tag") else "dockerfile", tokens[i + 1])
            i += 2
            continue
        for flag, key in (("--tag=", "image"), ("--file=", "dockerfile")):
            if t.startswith(flag):
                out.setdefault(key, t[len(flag):])
        if t.startswith("-"):
            i += 2 if t in ("--build-arg", "--target", "--platform", "--label", "--cache-from", "--secret") else 1
            continue
        positional.append(t)
        i += 1
    if "image" not in out:
        return None
    out["context"] = positional[-1] if positional else "."
    return out


def _image_builds(jobs: dict) -> list[dict[str, str]]:
    """The images a workflow builds: repository (no tag), Dockerfile and context, as declared."""
    found = []
    for job in jobs.values():
        for step in (job.get("steps") if isinstance(job, dict) and isinstance(job.get("steps"), list) else []):
            if not isinstance(step, dict):
                continue
            if isinstance(step.get("run"), str):
                for m in _DOCKER_BUILD.finditer(step["run"]):
                    args = _docker_build_args(m.group(1))
                    if args:
                        found.append(args)
            uses, with_ = step.get("uses"), step.get("with")
            if isinstance(uses, str) and uses.startswith("docker/build-push-action") and isinstance(with_, dict) and isinstance(with_.get("tags"), str):
                for tag in re.split(r"[\s,]+", with_["tags"].strip()):
                    if tag:
                        found.append({"image": tag, "dockerfile": with_.get("file") if isinstance(with_.get("file"), str) else None, "context": with_.get("context") if isinstance(with_.get("context"), str) else "."})
    builds = []
    for b in found:
        if "${{" in b["image"].split(":", 1)[0]:  # the repository itself is computed: nothing to match on
            continue
        context = posixpath.normpath(b["context"]) if "${{" not in b["context"] and "://" not in b["context"] else None
        dockerfile = b.get("dockerfile") or (posixpath.join(context, "Dockerfile") if context is not None else None)
        row = {"image": image_repository(b["image"]), "context": context, "dockerfile": posixpath.normpath(dockerfile) if dockerfile and "${{" not in dockerfile else None}
        if row not in builds:
            builds.append(row)
    return sorted(builds, key=lambda r: (r["image"], r["dockerfile"] or ""))


class GithubWorkflow:
    id = "infra.github_actions"
    version = "1.1.0"

    def matches(self, path: str) -> bool:
        return is_workflow(path)

    def extract(self, path, data, blob):
        try:
            docs = load_yaml_documents(data)
        except YAML_ERRORS as exc:
            return _unparseable(self.id, "workflow YAML", exc)
        doc = docs[0] if docs else None
        if not isinstance(doc, dict):
            return Extraction(diagnostics=[diagnostic(self.id, "warning", "workflow is not a mapping")], parsed=False)
        declared = doc.get("name")
        name = declared if isinstance(declared, str) and declared else posixpath.splitext(posixpath.basename(path))[0]
        on = doc.get("on", doc.get(True))  # YAML 1.1: the bare key `on` loads as boolean True
        triggers = [on] if isinstance(on, str) else _strs(on) if isinstance(on, list) else [k for k in on if isinstance(k, str)] if isinstance(on, dict) else []
        jobs = doc.get("jobs") if isinstance(doc.get("jobs"), dict) else {}
        at = (yaml_key_span(data, "name", declared) if isinstance(declared, str) else None) or yaml_key_span(data, "on") or whole_file(data)
        builds = _image_builds(jobs)
        attributes: dict[str, Any] = {"triggers": sorted(set(triggers)), "jobs": sorted(str(j) for j in jobs)}
        if builds:
            attributes["image_builds"] = builds
        out = [entity(blob, at, K.PIPELINE, {"path": path, "name": name}, name, attributes, E1)]
        for scope in [doc] + [j for j in jobs.values() if isinstance(j, dict)] + [s for j in jobs.values() if isinstance(j, dict) for s in (j.get("steps") if isinstance(j.get("steps"), list) else []) if isinstance(s, dict)]:
            env = scope.get("env")
            for var, value in sorted(env.items(), key=lambda kv: str(kv[0])) if isinstance(env, dict) else []:
                m = _ACTIONS_VAR.match(value.strip()) if isinstance(var, str) and isinstance(value, str) else None
                if m and _ENV_NAME.match(var):
                    var_at = yaml_key_span(data, var) or at
                    out.append(entity(blob, var_at, K.CONFIG_KEY, {"name": var}, var, {}, E1))
                    out += _alias(blob, var_at, var, m.group(2), {"secrets": "github_secret", "vars": "github_variable", "env": "workflow_env"}[m.group(1)])
        seen: set[str] = set()
        for job in jobs.values():
            env = job.get("environment") if isinstance(job, dict) else None
            env = env.get("name") if isinstance(env, dict) else env
            if isinstance(env, str) and env and "${{" not in env and env not in seen:  # an expression is not a name
                seen.add(env)
                env_at = yaml_key_span(data, "environment", env) or yaml_key_span(data, "name", env, at.end_byte) or yaml_key_span(data, "environment") or at
                out.append(entity(blob, env_at, K.ENVIRONMENT, {"name": env}, env, {}, E1))
        return Extraction(out)


# --------------------------------------------------------------------------- Kubernetes

WORKLOAD_KINDS = {"Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob"}
_SEPARATOR = re.compile(rb"^---(?:[ \t].*)?\r?$", re.M)
_HAS_CONTENT = re.compile(rb"^[ \t]*[^#%\s]", re.M)


def document_ranges(data: bytes, count: int) -> list[tuple[int, int]]:
    """Byte range of each non-empty YAML document, or the whole file for all if they cannot be told apart."""
    cuts = [0] + [m.end() for m in _SEPARATOR.finditer(data)] + [len(data)]
    starts = [0] + [m.start() for m in _SEPARATOR.finditer(data)]
    chunks = [(a, b if i + 1 == len(cuts) - 1 else starts[i + 1]) for i, (a, b) in enumerate(zip(cuts, cuts[1:]))]
    chunks = [(a, b) for a, b in chunks if _HAS_CONTENT.search(data, a, b)]
    return chunks if len(chunks) == count else [(0, len(data))] * count


def _pod_spec(kind: str, doc: dict) -> dict:
    node: Any = doc.get("spec")
    for key in (["jobTemplate", "spec"] if kind == "CronJob" else []) + ["template", "spec"]:
        node = node.get(key) if isinstance(node, dict) else None
    return node if isinstance(node, dict) else {}


def _containers(pod: dict) -> list[dict]:
    return [c for key in ("initContainers", "containers") for c in (pod.get(key) if isinstance(pod.get(key), list) else []) if isinstance(c, dict)]


def _config_refs(blob: str, data: bytes, unit: dict, names: list[str], start: int, end: int, fallback: Span, key_span) -> list:
    out, seen = [], set()
    for name in names:
        if name in seen or not _ENV_NAME.match(name):
            continue
        seen.add(name)
        at = key_span(name, start, end) or fallback
        key = {"name": name}
        out.append(entity(blob, at, K.CONFIG_KEY, key, name, {}, E1))
        out.append(relation(blob, at, unit, Predicate.REFERENCES_CONFIG, ref(K.CONFIG_KEY, key, name), evidence_class=E1))
    return out


class Kubernetes:
    id = "infra.kubernetes"
    version = "1.0.0"

    def matches(self, path: str) -> bool:
        return path.lower().endswith(YAML_SUFFIXES) and not is_workflow(path) and not is_compose(path)

    def extract(self, path, data, blob):
        try:
            docs = load_yaml_documents(data)
        except YAML_ERRORS as exc:
            return _unparseable(self.id, "YAML", exc)
        docs = [d for d in docs if d is not None]
        out: list = []
        for doc, (start, end) in zip(docs, document_ranges(data, len(docs))):
            items = doc.get("items") if isinstance(doc, dict) and doc.get("kind") == "List" else None
            for manifest in items if isinstance(items, list) else [doc]:
                out += self._workload(blob, data, manifest, start, end)
        return Extraction(out)

    def _workload(self, blob, data, doc, start, end) -> list:
        if not (isinstance(doc, dict) and isinstance(doc.get("apiVersion"), str) and doc.get("kind") in WORKLOAD_KINDS):
            return []
        kind = doc["kind"]
        meta = doc.get("metadata") if isinstance(doc.get("metadata"), dict) else {}
        name, declared_ns = meta.get("name"), meta.get("namespace")
        if not isinstance(name, str) or not name:
            return []
        namespace = declared_ns if isinstance(declared_ns, str) and declared_ns else "default"
        containers = _containers(_pod_spec(kind, doc))
        images = list(dict.fromkeys(c["image"] for c in containers if isinstance(c.get("image"), str)))
        attributes: dict[str, Any] = {"images": images}
        replicas = doc["spec"].get("replicas") if isinstance(doc.get("spec"), dict) else None
        if isinstance(replicas, int) and not isinstance(replicas, bool):
            attributes["replicas"] = replicas
        whole = span(data, start, end)
        at = yaml_key_span(data, "name", name, start, end) or whole
        key = {"kind": f"k8s.{kind}", "namespace": namespace, "name": name}
        unit = ref(K.DEPLOYMENT_UNIT, key, name)
        out = [entity(blob, at, K.DEPLOYMENT_UNIT, key, name, attributes, E1)]
        if namespace == declared_ns:
            # Heuristic: a namespace is being read as an environment.
            ns_at = yaml_key_span(data, "namespace", namespace, start, end) or at
            out.append(entity(blob, ns_at, K.ENVIRONMENT, {"name": namespace}, namespace, {}, E4))
            out.append(relation(blob, ns_at, unit, Predicate.DEPLOYED_TO, ref(K.ENVIRONMENT, {"name": namespace}, namespace), evidence_class=E4))
        names = [e["name"] for c in containers for e in (c.get("env") if isinstance(c.get("env"), list) else []) if isinstance(e, dict) and isinstance(e.get("name"), str)]
        return out + _config_refs(blob, data, unit, names, at.end_byte, end, at, lambda n, a, b: yaml_key_span(data, "name", n, a, b))


# --------------------------------------------------------------------------- docker-compose


class Compose:
    id = "infra.compose"
    version = "1.1.0"

    def matches(self, path: str) -> bool:
        return is_compose(path)

    def extract(self, path, data, blob):
        try:
            docs = load_yaml_documents(data)
        except YAML_ERRORS as exc:
            return _unparseable(self.id, "compose YAML", exc)
        doc = docs[0] if docs else None
        services = doc.get("services") if isinstance(doc, dict) else None
        if not isinstance(services, dict):
            return Extraction(diagnostics=[diagnostic(self.id, "info", "no services mapping declared")])
        out: list = []
        section = yaml_key_span(data, "services")
        cursor = section.end_byte if section else 0
        for service, body in services.items():
            if not isinstance(service, str):
                continue
            body = body if isinstance(body, dict) else {}
            at = yaml_key_span(data, service, None, cursor) or section or whole_file(data)
            attributes: dict[str, Any] = {"build": "build" in body and body["build"] is not None}
            if isinstance(body.get("image"), str):
                attributes["image"] = body["image"]
            key = {"kind": "compose.Service", "namespace": "compose", "name": service}
            out.append(entity(blob, at, K.DEPLOYMENT_UNIT, key, service, attributes, E1))
            env = body.get("environment")
            names = [k for k in env if isinstance(k, str)] if isinstance(env, dict) else [e.split("=", 1)[0].strip() for e in _strs(env)]
            # Mapping form `K: v` or list form `- K=v` / `- K`; the span covers the name only.
            name_span = lambda n, a, b: search_span(data, rf"""^[ \t]*(?:-[ \t]*)?["']?({re.escape(n)})["']?[ \t]*(?:[:=]|\r?$)""", a, b, 1)
            unit = ref(K.DEPLOYMENT_UNIT, key, service)
            out += _config_refs(blob, data, unit, names, at.end_byte, None, at, name_span)
            pairs = env.items() if isinstance(env, dict) else [tuple(e.split("=", 1)) for e in _strs(env) if "=" in e]
            for var, value in pairs:
                m = _COMPOSE_VAR.match(value.strip()) if isinstance(var, str) and isinstance(value, str) else None
                if m:
                    out += _alias(blob, name_span(var.strip(), at.end_byte, None) or at, var.strip(), m.group(1) or m.group(2), "compose_interpolation")
            dockerfile = _compose_dockerfile(path, body.get("build"))
            if dockerfile:
                build_at = yaml_key_span(data, "build", None, at.end_byte) or at
                out.append(relation(blob, build_at, unit, Predicate.BUILT_BY, ref(K.BUILD_TARGET, {"path": dockerfile}, dockerfile, declare=False), {"basis": "compose_build"}, E1))
        return Extraction(out)


def _compose_dockerfile(path: str, build: Any) -> str | None:
    """Repository path of the Dockerfile a compose `build:` names (context relative to the compose file)."""
    context, dockerfile = (build, None) if isinstance(build, str) else (build.get("context", "."), build.get("dockerfile")) if isinstance(build, dict) else (None, None)
    if not isinstance(context, str) or "://" in context or context.startswith("git@") or "$" in context:
        return None
    if dockerfile is not None and (not isinstance(dockerfile, str) or "$" in dockerfile):
        return None
    joined = posixpath.normpath(posixpath.join(posixpath.dirname(path), context, dockerfile or "Dockerfile"))
    return None if joined.startswith("..") else joined


# --------------------------------------------------------------------------- Terraform

_MODULE_DIR = re.compile(r"(?:^|/)modules/([^/]+)/")
_URL_USERINFO = re.compile(r"(://)[^/@\s]+@")


def _label(raw: Any) -> str:
    return raw.strip('"') if isinstance(raw, str) else str(raw)


class Terraform:
    id = "infra.terraform"
    version = "1.0.0"

    def matches(self, path: str) -> bool:
        return path.lower().endswith(".tf")

    def extract(self, path, data, blob):
        try:
            import hcl2

            doc = hcl2.loads(data.decode("utf-8"))
        except Exception as exc:  # hcl2 raises parser-specific errors (lark) that share no useful base class
            return Extraction(diagnostics=[diagnostic(self.id, "warning", f"unparseable HCL: {type(exc).__name__}")], parsed=False)
        if not isinstance(doc, dict):
            return Extraction(diagnostics=[diagnostic(self.id, "warning", "HCL document is not a mapping")], parsed=False)
        enclosing = _MODULE_DIR.findall(path)
        out: list = []
        for block in doc.get("resource") if isinstance(doc.get("resource"), list) else []:
            for raw_type, by_name in block.items() if isinstance(block, dict) else []:
                for raw_name in by_name if isinstance(by_name, dict) else []:
                    if raw_name == "__is_block__":
                        continue
                    rtype, name = _label(raw_type), _label(raw_name)
                    key = {"provider": "terraform", "type": rtype, "name": name}
                    if enclosing:
                        key["module"] = enclosing[-1]
                    at = search_span(data, rf'^[ \t]*(resource\s+"{re.escape(rtype)}"\s+"{re.escape(name)}")', group=1) or whole_file(data)
                    out.append(entity(blob, at, K.INFRASTRUCTURE_RESOURCE, key, f"{rtype}.{name}", {"resource_provider": rtype.split("_", 1)[0]}, E1))
        for block in doc.get("module") if isinstance(doc.get("module"), list) else []:
            for raw_name, body in block.items() if isinstance(block, dict) else []:
                if raw_name == "__is_block__":
                    continue
                name = _label(raw_name)
                source = body.get("source") if isinstance(body, dict) else None
                source = _URL_USERINFO.sub(r"\1", _label(source)) if isinstance(source, str) else None  # never keep URL credentials
                at = search_span(data, rf'^[ \t]*(module\s+"{re.escape(name)}")', group=1) or whole_file(data)
                out.append(obs("terraform_module_call", blob, at, {"name": name, "source": source}, E1))
        return Extraction(out)


EXTRACTORS = [Dockerfile(), GithubWorkflow(), Kubernetes(), Compose(), Terraform()]
