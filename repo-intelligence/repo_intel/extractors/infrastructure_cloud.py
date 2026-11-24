"""Cloud infrastructure as code beyond Terraform: Bicep, CloudFormation and AWS SAM,
Helm charts, Pulumi and the Serverless Framework.

Like Terraform, resources are InfrastructureResource entities keyed
{provider, type, name}, and no argument VALUE is copied: only resource types, logical
names, and the physical names of queues, topics and functions, which are what code
refers to. A queue or topic whose physical name is a literal is also declared as an
EventTopic, so code that sends to it links to the declaration. Functions (Lambda,
SAM, Serverless) and Helm charts are DeploymentUnits. Pulumi stack files and
Serverless/SAM stages are Environments.

Pulumi programs are read in TypeScript/JavaScript and Python: `new aws.s3.Bucket("x")`
or `aws.s3.Bucket("x")` from an imported Pulumi provider package declares a resource
named by the literal first argument.
"""

from __future__ import annotations

import ast
import json
import posixpath
import re
from typing import Any

from repo_intel.compile.ontology import EntityKind, EvidenceClass, Predicate
from repo_intel.extractors.base import Extraction, diagnostic, span
from repo_intel.extractors.infrastructure import YAML_ERRORS, YAML_SUFFIXES, is_compose, is_workflow, load_yaml_documents
from repo_intel.extractors.textscan import Doc, decode, matching, unquote, without_comments

E1 = EvidenceClass.E1
K = EntityKind
P = Predicate


def _literal(value: Any) -> str | None:
    return value if isinstance(value, str) and value and "${" not in value and "{{" not in value else None


def _pos(text: str, needle: str, start: int = 0) -> tuple[int, int] | None:
    i = text.find(needle, start)
    return (i, i + len(needle)) if i >= 0 else None


# AWS resource types whose physical name code uses to address them.
_AWS_TOPICS = {"AWS::SQS::Queue": ("QueueName", "sqs"), "AWS::SNS::Topic": ("TopicName", "sns")}
_AWS_FUNCTIONS = {"AWS::Lambda::Function", "AWS::Serverless::Function"}
_AWS_NAMES = {"AWS::DynamoDB::Table": "TableName", "AWS::S3::Bucket": "BucketName", "AWS::Lambda::Function": "FunctionName", "AWS::Serverless::Function": "FunctionName", "AWS::Serverless::SimpleTable": "TableName"}


def _cfn_resources(d: Doc, resources: dict, provider: str, namespace: str) -> None:
    cursor = 0
    for logical, body in resources.items():
        if not isinstance(logical, str) or not isinstance(body, dict) or not isinstance(body.get("Type"), str):
            continue
        rtype = body["Type"]
        props = body.get("Properties") if isinstance(body.get("Properties"), dict) else {}
        where = _pos(d.text, logical, cursor)
        cursor = where[1] if where else cursor
        at = d.at(*where) if where else d.whole()
        attrs: dict[str, Any] = {"resource_provider": rtype.split("::")[0].lower()}
        name_key = _AWS_NAMES.get(rtype) or (_AWS_TOPICS.get(rtype) or (None,))[0]
        physical = _literal(props.get(name_key)) if name_key else None
        if physical:
            attrs["physical_name"] = physical
        d.entity(at, K.INFRASTRUCTURE_RESOURCE, {"provider": provider, "type": rtype, "name": logical}, logical, attrs, E1)
        if rtype in _AWS_TOPICS and physical:
            d.entity(at, K.EVENT_TOPIC, {"name": physical}, physical, {"transport": _AWS_TOPICS[rtype][1], "declared_in": provider}, E1)
        if rtype in _AWS_FUNCTIONS:
            fn_attrs = {k: props[k] for k in ("Handler", "Runtime", "CodeUri") if isinstance(props.get(k), str)}
            events = props.get("Events") if isinstance(props.get("Events"), dict) else {}
            kinds = sorted({e.get("Type") for e in events.values() if isinstance(e, dict) and isinstance(e.get("Type"), str)})
            name = physical or logical
            d.entity(at, K.DEPLOYMENT_UNIT, {"kind": "aws.Lambda", "namespace": namespace, "name": name}, name, {**{k.lower(): v for k, v in fn_attrs.items()}, **({"events": kinds} if kinds else {})}, E1)


# ------------------------------------------------------------- CloudFormation


class CloudFormation:
    id = "infra.cloudformation"
    version = "1.0.0"

    def matches(self, path: str) -> bool:
        return path.lower().endswith(YAML_SUFFIXES + (".json", ".template")) and not is_workflow(path) and not is_compose(path) and posixpath.basename(path).lower() not in ("serverless.yml", "serverless.yaml")

    def extract(self, path, data, blob):
        if b"AWS::" not in data or (b"Resources" not in data):
            return Extraction(parsed=False)
        try:
            doc = json.loads(data.decode("utf-8")) if path.lower().endswith(".json") else (load_yaml_documents(data) or [None])[0]
        except YAML_ERRORS as exc:
            return Extraction(diagnostics=[diagnostic(self.id, "warning", f"unparseable template: {type(exc).__name__}")], parsed=False)
        if not isinstance(doc, dict) or not isinstance(doc.get("Resources"), dict):
            return Extraction(parsed=False)
        d = Doc(path, data, blob, data.decode("utf-8"))
        stack = posixpath.splitext(posixpath.basename(path))[0]
        sam = "AWS::Serverless" in str(doc.get("Transform", ""))
        _cfn_resources(d, doc["Resources"], "sam" if sam else "cloudformation", stack)
        return Extraction(d.out, parsed=bool(d.out))


# ---------------------------------------------------------------------- Bicep


_BICEP_RESOURCE = re.compile(r"(?m)^\s*resource\s+(\w+)\s+'([^'@]+)@([^']+)'\s*(existing\s*)?=\s*(?:\[[^\]]*\]\s*)?(?:if\s*\([^)]*\)\s*)?\{")
_BICEP_MODULE = re.compile(r"(?m)^\s*module\s+(\w+)\s+'([^']+)'\s*=")
_BICEP_NAME = re.compile(r"(?m)^\s*name\s*:\s*('[^'\n]*')\s*$")


class Bicep:
    id = "infra.bicep"
    version = "1.0.0"

    def matches(self, path: str) -> bool:
        return path.lower().endswith(".bicep")

    def extract(self, path, data, blob):
        raw = decode(data)
        if raw is None:
            return Extraction(diagnostics=[diagnostic(self.id, "warning", "undecodable text")], parsed=False)
        text = without_comments(raw, "javascript")
        d = Doc(path, data, blob, text)
        for m in _BICEP_RESOURCE.finditer(text):
            symbol, rtype, api, existing = m.group(1), m.group(2), m.group(3), m.group(4)
            body_end = matching(text, m.end() - 1)
            literal = None
            for n in _BICEP_NAME.finditer(text, m.end(), body_end):
                if text[m.end(): n.start()].count("{") == text[m.end(): n.start()].count("}"):
                    literal = unquote(n.group(1))
                    break
            attrs = {"resource_provider": rtype.split(".")[0].lower(), "api_version": api, **({"physical_name": literal} if literal and "${" not in literal else {}), **({"existing": True} if existing else {})}
            at = d.at(m.start(1), m.end(1))
            d.entity(at, K.INFRASTRUCTURE_RESOURCE, {"provider": "bicep", "type": rtype, "name": symbol}, symbol, attrs, E1)
            if rtype.lower() in ("microsoft.servicebus/namespaces/queues", "microsoft.servicebus/namespaces/topics", "microsoft.eventhub/namespaces/eventhubs") and literal and "${" not in literal:
                transport = "eventhubs" if "eventhub" in rtype.lower() else "servicebus"
                d.entity(at, K.EVENT_TOPIC, {"name": literal}, literal, {"transport": transport, "declared_in": "bicep"}, E1)
        for m in _BICEP_MODULE.finditer(text):
            d.observe("iac_module_call", d.at(m.start(1), m.end(1)), {"tool": "bicep", "name": m.group(1), "source": m.group(2)}, E1)
        return Extraction(d.out, parsed=True)


# ----------------------------------------------------------------------- Helm


class HelmChart:
    id = "infra.helm"
    version = "1.0.0"

    def matches(self, path: str) -> bool:
        return posixpath.basename(path) in ("Chart.yaml", "Chart.yml")

    def extract(self, path, data, blob):
        try:
            doc = (load_yaml_documents(data) or [None])[0]
        except YAML_ERRORS as exc:
            return Extraction(diagnostics=[diagnostic(self.id, "warning", f"unparseable chart: {type(exc).__name__}")], parsed=False)
        if not isinstance(doc, dict) or not _literal(doc.get("name")):
            return Extraction(diagnostics=[diagnostic(self.id, "info", "chart has no name")])
        d = Doc(path, data, blob, data.decode("utf-8"))
        name = doc["name"]
        deps = [dep["name"] for dep in doc.get("dependencies") or [] if isinstance(dep, dict) and _literal(dep.get("name"))]
        attrs = {k: str(doc[s]) for k, s in (("chart_version", "version"), ("app_version", "appVersion"), ("chart_type", "type")) if doc.get(s) is not None}
        where = _pos(d.text, name)
        d.entity(d.at(*where) if where else d.whole(), K.DEPLOYMENT_UNIT, {"kind": "helm.Chart", "namespace": "helm", "name": name}, name, {**attrs, **({"dependencies": deps} if deps else {})}, E1)
        return Extraction(d.out)


# --------------------------------------------------------------- Serverless


class Serverless:
    id = "infra.serverless"
    version = "1.0.0"

    def matches(self, path: str) -> bool:
        return posixpath.basename(path).lower() in ("serverless.yml", "serverless.yaml")

    def extract(self, path, data, blob):
        try:
            doc = (load_yaml_documents(data) or [None])[0]
        except YAML_ERRORS as exc:
            return Extraction(diagnostics=[diagnostic(self.id, "warning", f"unparseable serverless.yml: {type(exc).__name__}")], parsed=False)
        if not isinstance(doc, dict):
            return Extraction(parsed=False)
        service = doc.get("service")
        service = service.get("name") if isinstance(service, dict) else service
        service = _literal(service) or posixpath.basename(posixpath.dirname(path)) or "serverless"
        provider = doc.get("provider") if isinstance(doc.get("provider"), dict) else {}
        d = Doc(path, data, blob, data.decode("utf-8"))
        cursor = 0
        for fn, body in (doc.get("functions") or {}).items() if isinstance(doc.get("functions"), dict) else []:
            if not isinstance(fn, str) or not isinstance(body, dict):
                continue
            where = _pos(d.text, fn + ":", cursor)
            cursor = where[1] if where else cursor
            at = d.at(where[0], where[1] - 1) if where else d.whole()
            events, topics = [], []
            for e in body.get("events") if isinstance(body.get("events"), list) else []:
                if not isinstance(e, dict) or not e:
                    continue
                kind, spec = next(iter(e.items()))
                events.append(kind)
                if kind in ("http", "httpApi") and isinstance(spec, dict) and _literal(spec.get("path")):
                    route = "/" + spec["path"].lstrip("/")
                    method = str(spec.get("method", "ANY")).upper()
                    topics.append(("route", f"{method} {route}"))
                elif kind in ("http", "httpApi") and isinstance(spec, str):
                    topics.append(("route", spec.upper()))
                elif kind in ("sqs", "sns"):
                    raw = spec.get("arn") or spec.get("topicName") if isinstance(spec, dict) else spec
                    name = raw.rsplit(":", 1)[-1] if _literal(raw) else None
                    if name:
                        topics.append((kind, name))
            attrs = {"handler": body["handler"]} if isinstance(body.get("handler"), str) else {}
            if isinstance(provider.get("runtime"), str):
                attrs["runtime"] = body.get("runtime") if isinstance(body.get("runtime"), str) else provider["runtime"]
            if events:
                attrs["events"] = sorted(set(events))
            routes = [t[1] for t in topics if t[0] == "route"]
            if routes:
                attrs["routes"] = routes
            triggers = [t[1] for t in topics if t[0] in ("sqs", "sns")]
            if triggers:
                attrs["triggered_by"] = triggers
            d.entity(at, K.DEPLOYMENT_UNIT, {"kind": "aws.Lambda", "namespace": service, "name": fn}, fn, attrs, E1)
        stage = provider.get("stage")
        if _literal(stage):
            where = _pos(d.text, "stage:")
            d.entity(d.at(*where) if where else d.whole(), K.ENVIRONMENT, {"name": stage}, stage, {}, E1)
        resources = doc.get("resources") if isinstance(doc.get("resources"), dict) else {}
        if isinstance(resources.get("Resources"), dict):
            _cfn_resources(d, resources["Resources"], "cloudformation", service)
        return Extraction(d.out)


# --------------------------------------------------------------------- Pulumi


_PULUMI_TS_IMPORT = re.compile(r"""import\s+\*\s+as\s+(\w+)\s+from\s+['"]@pulumi/(\w[\w-]*)['"]|(?:const|let)\s+(\w+)\s*=\s*require\(\s*['"]@pulumi/(\w[\w-]*)['"]\s*\)""")
_PULUMI_TS_NEW = re.compile(r"""\bnew\s+(\w+)((?:\.\w+)+)\(\s*(['"`][^'"`\n]+['"`])""")


class Pulumi:
    id = "infra.pulumi"
    version = "1.0.0"
    _CODE = (".ts", ".js", ".mjs", ".py")

    def matches(self, path: str) -> bool:
        base = posixpath.basename(path)
        return bool(re.fullmatch(r"Pulumi(\.[\w-]+)?\.ya?ml", base)) or (path.lower().endswith(self._CODE) and not path.endswith(".d.ts"))

    def extract(self, path, data, blob):
        base = posixpath.basename(path)
        if base.startswith("Pulumi."):
            return self._yaml(path, data, blob, base)
        if b"pulumi" not in data:
            return Extraction(parsed=False)
        text = decode(data)
        if text is None:
            return Extraction(parsed=False)
        d = Doc(path, data, blob, text)
        if path.endswith(".py"):
            self._python(d)
        else:
            d.text = without_comments(text, "typescript")
            aliases = {(m.group(1) or m.group(3)): (m.group(2) or m.group(4)) for m in _PULUMI_TS_IMPORT.finditer(d.text)}
            for m in _PULUMI_TS_NEW.finditer(d.text):
                if m.group(1) in aliases and aliases[m.group(1)] != "pulumi":
                    name = unquote(m.group(3))
                    if name:
                        rtype = aliases[m.group(1)] + m.group(2)
                        d.entity(d.at(m.start(3), m.end(3)), K.INFRASTRUCTURE_RESOURCE, {"provider": "pulumi", "type": rtype, "name": name}, name, {"resource_provider": aliases[m.group(1)]}, EvidenceClass.E2)
        return Extraction(d.out, parsed=bool(d.out))

    def _python(self, d: Doc) -> None:
        try:
            tree = ast.parse(d.text)
        except (SyntaxError, ValueError, RecursionError):
            return
        aliases: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.name.startswith("pulumi_"):
                        aliases[a.asname or a.name] = a.name[len("pulumi_"):]
            elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith("pulumi_"):
                for a in node.names:
                    aliases[a.asname or a.name] = f"{node.module[len('pulumi_'):]}.{a.name}"
        starts = [0]
        for line in d.data.splitlines(keepends=True):
            starts.append(starts[-1] + len(line))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
                continue
            chain, f = [], node.func
            while isinstance(f, ast.Attribute):
                chain.insert(0, f.attr)
                f = f.value
            if not isinstance(f, ast.Name) or f.id not in aliases or not chain or not chain[-1][:1].isupper():
                continue
            rtype = ".".join([aliases[f.id], *chain])
            a = node.args[0]
            at = span(d.data, starts[a.lineno - 1] + a.col_offset, starts[(a.end_lineno or a.lineno) - 1] + (a.end_col_offset or a.col_offset))
            d.entity(at, K.INFRASTRUCTURE_RESOURCE, {"provider": "pulumi", "type": rtype, "name": a.value}, a.value, {"resource_provider": rtype.split(".")[0]}, EvidenceClass.E2)

    def _yaml(self, path, data, blob, base) -> Extraction:
        try:
            doc = (load_yaml_documents(data) or [None])[0]
        except YAML_ERRORS as exc:
            return Extraction(diagnostics=[diagnostic(self.id, "warning", f"unparseable Pulumi file: {type(exc).__name__}")], parsed=False)
        d = Doc(path, data, blob, data.decode("utf-8"))
        stack = re.fullmatch(r"Pulumi\.([\w-]+)\.ya?ml", base)
        if stack:  # Pulumi.<stack>.yaml: the stack's settings; only its name is kept
            d.entity(d.whole(), K.ENVIRONMENT, {"name": stack.group(1)}, stack.group(1), {"source": "pulumi_stack"}, E1)
            return Extraction(d.out)
        if not isinstance(doc, dict):
            return Extraction(parsed=False)
        cursor = 0
        for name, body in (doc.get("resources") or {}).items() if isinstance(doc.get("resources"), dict) else []:
            if isinstance(name, str) and isinstance(body, dict) and isinstance(body.get("type"), str):
                where = _pos(d.text, name + ":", cursor)
                cursor = where[1] if where else cursor
                rtype = body["type"].replace(":", ".")
                d.entity(d.at(where[0], where[1] - 1) if where else d.whole(), K.INFRASTRUCTURE_RESOURCE, {"provider": "pulumi", "type": rtype, "name": name}, name, {"resource_provider": rtype.split(".")[0]}, E1)
        return Extraction(d.out)


EXTRACTORS = [CloudFormation(), Bicep(), HelmChart(), Serverless(), Pulumi()]
