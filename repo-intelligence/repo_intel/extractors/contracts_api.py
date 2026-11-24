"""API contracts beyond OpenAPI: protobuf/gRPC, GraphQL SDL and AsyncAPI, and the
code that implements gRPC services and GraphQL resolvers in Python and TypeScript.

As with OpenAPI, a contract yields an Interface and one "contract_operation" per
operation, never an Endpoint; the repo-level linker matches operations to the
endpoints code implements, by method and route:

- gRPC: method "RPC", route "/<Service>/<Method>" (the proto package is an
  attribute: generated code names services without it).
- GraphQL: method "QUERY", "MUTATION" or "SUBSCRIPTION", route the root field name.

Messages, enums and GraphQL object types are DataSchema entities
({kind: protobuf|graphql, name}). AsyncAPI channels are EventTopic entities the
Interface CONTAINS; their transport comes from the document's servers.

Implementations (Python, TypeScript and JavaScript; other languages are read by
their own extractors): a class deriving from a generated `<Service>Servicer` /
`<Service>ImplBase` / `<Service>Base`, or a server built with
`add<Service>Servicer_to_server` / `server.addService(<x>.<Service>.service, {...})`,
implements each method it defines; GraphQL resolvers from Strawberry, Ariadne,
Graphene and Apollo-style resolver maps implement their root fields.
"""

from __future__ import annotations

import ast
import json
import posixpath
import re

from repo_intel.compile.ontology import EntityKind, EvidenceClass, Predicate
from repo_intel.extractors.base import Extraction, diagnostic, span
from repo_intel.extractors.infrastructure import YAML_ERRORS, YAML_SUFFIXES, is_compose, is_workflow, load_yaml_documents
from repo_intel.extractors.textscan import Doc, blank, decode, matching, without_comments

E1 = EvidenceClass.E1
K = EntityKind
P = Predicate


# ------------------------------------------------------------------ protobuf


_PROTO_PACKAGE = re.compile(r"\bpackage\s+([\w.]+)\s*;")
_PROTO_BLOCK = re.compile(r"\b(service|message|enum)\s+(\w+)\s*\{")
_PROTO_RPC = re.compile(r"\brpc\s+(\w+)\s*\(\s*(stream\s+)?([\w.]+)\s*\)\s*returns\s*\(\s*(stream\s+)?([\w.]+)\s*\)")
_PROTO_IMPORT = re.compile(r"\bimport\s+(?:public\s+|weak\s+)?\"([^\"]+)\"")


class Protobuf:
    id = "contract.protobuf"
    version = "1.0.0"

    def matches(self, path: str) -> bool:
        return path.lower().endswith(".proto")

    def extract(self, path, data, blob):
        raw = decode(data)
        if raw is None:
            return Extraction(diagnostics=[diagnostic(self.id, "warning", "undecodable text")], parsed=False)
        text = without_comments(raw, "protobuf")
        d = Doc(path, data, blob, text)
        pkg = m.group(1) if (m := _PROTO_PACKAGE.search(text)) else ""
        qualify = lambda n: f"{pkg}.{n}" if pkg else n
        imports = [i.group(1) for i in _PROTO_IMPORT.finditer(text)]
        services = []

        def walk(start: int, end: int, prefix: str) -> None:
            i = start
            while (m := _PROTO_BLOCK.search(text, i, end)) is not None:
                close = matching(text, m.end() - 1)
                kind, name = m.group(1), m.group(2)
                at = d.at(m.start(2), m.end(2))
                if kind == "service":
                    services.append((name, m.end(), close, at))
                else:
                    full = qualify(prefix + name)
                    d.entity(at, K.DATA_SCHEMA, {"kind": "protobuf", "name": full}, full, {"proto_kind": kind, "file": path}, E1)
                    if kind == "message":
                        walk(m.end(), close - 1, f"{prefix}{name}.")
                i = close
        walk(0, len(text), "")
        for name, a, b, at in services:
            full = qualify(name)
            iface = d.entity(at, K.INTERFACE, {"path": path, "title": full}, full, {"spec": "grpc", "package": pkg or None, "imports": imports}, E1)
            for rpc in _PROTO_RPC.finditer(text, a, b):
                streaming = "bidi" if rpc.group(2) and rpc.group(4) else "client" if rpc.group(2) else "server" if rpc.group(4) else None
                d.observe("contract_operation", d.at(rpc.start(1), rpc.end(1)), {
                    "method": "RPC", "route": f"/{name}/{rpc.group(1)}", "operation_id": f"{full}.{rpc.group(1)}", "protocol": "grpc",
                    "request": rpc.group(3), "response": rpc.group(5), **({"streaming": streaming} if streaming else {}),
                }, E1)
        if not services and not d.out:
            return Extraction(diagnostics=[diagnostic(self.id, "info", "no service, message or enum")])
        return Extraction(d.out)


# ------------------------------------------------------------------- GraphQL


_GQL_BLOCK = re.compile(r"\b(extend\s+)?(type|interface|input|enum|union|scalar|schema)\b\s*(\w*)[^{=\n]*([{=]?)")
_GQL_FIELD = re.compile(r"(?m)^[ \t]*(\w+)\s*(\(|:)")
_GQL_ROOT = re.compile(r"\b(query|mutation|subscription)\s*:\s*(\w+)")
_GQL_STRINGS = re.compile(r'"""(?:.|\n)*?"""|"(?:\\.|[^"\\\n])*"')
_GQL_SUFFIXES = (".graphql", ".graphqls", ".gql")


def graphql_operations(text: str) -> tuple[dict[str, str], list[tuple[str, str, int, int]], list[tuple[str, str, int, int]]]:
    """(root type name -> operation kind, [(kind, field, start, end)], [(type kind, name, start, end)])."""
    roots = {"Query": "QUERY", "Mutation": "MUTATION", "Subscription": "SUBSCRIPTION"}
    blocks = []
    i = 0
    while (m := _GQL_BLOCK.search(text, i)) is not None:
        end = matching(text, m.end() - 1) if m.group(4) == "{" else m.end()
        blocks.append((m, end))
        i = max(end, m.end())
    for m, end in blocks:
        if m.group(2) == "schema":
            roots = {name: op.upper() for op, name in _GQL_ROOT.findall(text[m.end(): end])} or roots
    ops, types = [], []
    for m, end in blocks:
        kind, name = m.group(2), m.group(3)
        if kind == "schema" or not name:
            continue
        if name in roots and kind in ("type",) and m.group(4) == "{":
            body_start = m.end()
            depth_text = text[body_start: end - 1]
            for f in _GQL_FIELD.finditer(depth_text):
                # Only fields at the top of the block: skip anything inside an argument list.
                if depth_text[: f.start()].count("(") == depth_text[: f.start()].count(")"):
                    ops.append((roots[name], f.group(1), body_start + f.start(1), body_start + f.end(1)))
        elif not m.group(1):  # `extend type X` adds to a type declared elsewhere
            types.append((kind, name, m.start(3), m.end(3)))
    return roots, ops, types


class GraphQLSchema:
    id = "contract.graphql"
    version = "1.0.0"

    def matches(self, path: str) -> bool:
        return path.lower().endswith(_GQL_SUFFIXES)

    def extract(self, path, data, blob):
        raw = decode(data)
        if raw is None:
            return Extraction(diagnostics=[diagnostic(self.id, "warning", "undecodable text")], parsed=False)
        text = blank(without_comments(raw, "graphql"), _GQL_STRINGS)
        d = Doc(path, data, blob, text)
        roots, ops, types = graphql_operations(text)
        if not ops and not types:
            return Extraction(diagnostics=[diagnostic(self.id, "info", "no type definitions")])
        title = posixpath.splitext(posixpath.basename(path))[0]
        if ops:
            d.entity(d.at(ops[0][2], ops[0][3]), K.INTERFACE, {"path": path, "title": title}, title, {"spec": "graphql"}, E1)
        for kind, name, a, b in ops:
            d.observe("contract_operation", d.at(a, b), {"method": kind, "route": name, "operation_id": name, "protocol": "graphql"}, E1)
        for kind, name, a, b in types:
            if name not in roots and kind != "scalar":
                d.entity(d.at(a, b), K.DATA_SCHEMA, {"kind": "graphql", "name": name}, name, {"graphql_kind": kind, "file": path}, E1)
        return Extraction(d.out)


# ------------------------------------------------------------------ AsyncAPI


_ASYNC_YAML = re.compile(rb"""^["']?asyncapi["']?\s*:""", re.M)
_ASYNC_JSON = re.compile(rb'"asyncapi"\s*:')


class AsyncAPI:
    id = "contract.asyncapi"
    version = "1.0.0"

    def matches(self, path: str) -> bool:
        return path.lower().endswith(YAML_SUFFIXES + (".json",)) and not is_workflow(path) and not is_compose(path)

    def extract(self, path, data, blob):
        is_json = path.lower().endswith(".json")
        if not (_ASYNC_JSON if is_json else _ASYNC_YAML).search(data):
            return Extraction(parsed=False)
        try:
            doc = json.loads(data.decode("utf-8")) if is_json else (load_yaml_documents(data) or [None])[0]
        except YAML_ERRORS as exc:
            return Extraction(diagnostics=[diagnostic(self.id, "warning", f"unparseable contract: {type(exc).__name__}")], parsed=False)
        if not isinstance(doc, dict) or "asyncapi" not in doc:
            return Extraction(parsed=False)
        text = data.decode("utf-8")
        d = Doc(path, data, blob, text)
        info = doc.get("info") if isinstance(doc.get("info"), dict) else {}
        title = info.get("title") if isinstance(info.get("title"), str) and info["title"] else posixpath.basename(path)
        servers = doc.get("servers") if isinstance(doc.get("servers"), dict) else {}
        protocols = sorted({s.get("protocol") for s in servers.values() if isinstance(s, dict) and isinstance(s.get("protocol"), str)})
        transport = protocols[0] if len(protocols) == 1 else "unknown"
        first = text.find("asyncapi")
        iface = d.entity(d.at(first, first + 8), K.INTERFACE, {"path": path, "title": title}, title, {"spec": "asyncapi", "version": str(doc["asyncapi"]), "protocols": protocols}, E1)
        channels = doc.get("channels") if isinstance(doc.get("channels"), dict) else {}
        cursor = 0
        for key, channel in channels.items():
            if not isinstance(key, str):
                continue
            # AsyncAPI 3 keys channels by id and gives the broker name as `address`.
            address = channel.get("address") if isinstance(channel, dict) and isinstance(channel.get("address"), str) else key
            if "{" in address:
                d.unresolved += 1
                continue
            pos = text.find(key, cursor)
            cursor = pos + len(key) if pos >= 0 else cursor
            at = d.at(pos, pos + len(key)) if pos >= 0 else d.whole()
            roles = sorted(r for r in ("publish", "subscribe") if isinstance(channel, dict) and r in channel)
            topic = d.entity(at, K.EVENT_TOPIC, {"name": address}, address, {"transport": transport, "declared_in": "asyncapi", **({"operations": roles} if roles else {})}, E1)
            d.relation(at, iface, P.CONTAINS, topic, {}, E1)
        return Extraction(d.out)


# ------------------------------------------------- implementations in Python


_PY_GRPC_BASE = re.compile(r"(\w+)Servicer$")
_PY_ADD = re.compile(r"^add_(\w+)Servicer_to_server$")


def _base_name(node: ast.expr) -> str:
    return node.attr if isinstance(node, ast.Attribute) else node.id if isinstance(node, ast.Name) else ""


def _const(node: ast.AST | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _py_impls(tree: ast.Module, d: Doc, line_starts: list[int]) -> None:
    # ast offsets are UTF-8 bytes within a line; `line_starts` are byte offsets of lines.
    at = lambda n: span(d.data, line_starts[n.lineno - 1] + n.col_offset, line_starts[(n.end_lineno or n.lineno) - 1] + (n.end_col_offset or n.col_offset))
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        methods = [n for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and not n.name.startswith("_")]
        for base in node.bases:
            m = _PY_GRPC_BASE.search(_base_name(base))
            if m and isinstance(base, ast.Attribute):  # pb2_grpc.<Service>Servicer
                for fn in methods:
                    d.endpoint(at(fn), "RPC", f"/{m.group(1)}/{fn.name}", "grpc", f"{node.name}.{fn.name}", protocol="grpc")
            if _base_name(base) == "ObjectType" and node.name in ("Query", "Mutation", "Subscription"):  # graphene
                op = node.name.upper()
                fields = {t.id for n in node.body if isinstance(n, ast.Assign) for t in n.targets if isinstance(t, ast.Name)}
                fields |= {fn.name[len("resolve_"):] for fn in methods if fn.name.startswith("resolve_")}
                for f in sorted(fields):
                    d.endpoint(at(node), op, _camel(f), "graphene", f"{node.name}.{f}", protocol="graphql")
        for dec in node.decorator_list:  # @strawberry.type class Query:
            target = dec.func if isinstance(dec, ast.Call) else dec
            if isinstance(target, ast.Attribute) and target.attr == "type" and _base_name(target.value) == "strawberry" and node.name in ("Query", "Mutation", "Subscription"):
                for item in node.body:
                    decs = item.decorator_list if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) else []
                    is_field = any(isinstance(x, (ast.Attribute, ast.Call)) and _base_name(x.func if isinstance(x, ast.Call) else x) in ("field", "mutation", "subscription") for x in decs)
                    name = item.name if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and is_field else item.target.id if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name) else None
                    if name:
                        d.endpoint(at(item), node.name.upper(), _camel(name), "strawberry", f"{node.name}.{name}", protocol="graphql")
    # Ariadne: query = QueryType(); @query.field("orders") / query.set_field("orders", fn)
    roots: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            ctor = _base_name(node.value.func)
            if ctor in ("QueryType", "MutationType", "SubscriptionType"):
                roots[node.targets[0].id] = ctor[: -len("Type")].upper()
            elif ctor == "ObjectType" and (name := _const(node.value.args[0] if node.value.args else None)) in ("Query", "Mutation", "Subscription"):
                roots[node.targets[0].id] = name.upper()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ("field", "set_field") and _base_name(node.func.value) in roots:
            field = _const(node.args[0] if node.args else None)
            if field:
                d.endpoint(at(node), roots[_base_name(node.func.value)], field, "ariadne", field, protocol="graphql")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and (m := _PY_ADD.match(node.func.attr)):
            d.observe("grpc_registration", at(node), {"service": m.group(1)})


def _camel(name: str) -> str:
    """Strawberry and Graphene expose snake_case Python names in camelCase."""
    head, *rest = name.split("_")
    return head + "".join(p[:1].upper() + p[1:] for p in rest)


# --------------------------------------------- implementations in TypeScript


_TS_GRPC_ADD_SERVICE = re.compile(r"\.addService\(\s*([\w.]+)\.service\s*,\s*\{")
_TS_RESOLVER_ROOT = re.compile(r"\b(Query|Mutation|Subscription)\s*:\s*\{")
_TS_NEST = re.compile(r"@(Query|Mutation|Subscription)\(\s*(?:\(\)\s*=>\s*[\w\[\]]+\s*)?(?:,\s*)?(?:\{[^}]*name\s*:\s*['\"](\w+)['\"][^}]*\})?\s*\)\s*(?:async\s+)?(\w+)\s*\(")


def _object_keys(text: str, open_at: int) -> list[tuple[str, int, int]]:
    """Top-level keys of the object literal whose `{` is at `open_at`: `k: v`,
    `k(...) {}`, `async k() {}` and shorthand `k`."""
    end = matching(text, open_at)
    keys, depth, i, quote, expect = [], 0, open_at, None, False
    ident = re.compile(r"\s*(?:async\s+)?(?:get\s+|set\s+)?([A-Za-z_$][\w$]*)\s*([:(,}])")
    while i < end:
        c = text[i]
        if quote:
            if c == "\\":
                i += 2
                continue
            if c == quote:
                quote = None
        elif c in "\"'`":
            quote = c
        elif c in "{[(":
            depth += 1
            expect = depth == 1 and c == "{"
        elif c in "}])":
            depth -= 1
        elif c == "," and depth == 1:
            expect = True
        if expect and depth == 1 and c in "{,":
            m = ident.match(text, i + 1)
            if m:
                keys.append((m.group(1), m.start(1), m.end(1)))
            expect = False
        i += 1
    return keys


def _ts_impls(text: str, d: Doc) -> None:
    for m in _TS_GRPC_ADD_SERVICE.finditer(text):
        service = m.group(1).split(".")[-1]
        if service.endswith("ServiceService"):  # grpc-tools names the definition <Service>Service
            service = service[: -len("Service")]
        brace = m.end() - 1
        for key, a, b in _object_keys(text, brace):
            d.endpoint(d.at(a, b), "RPC", f"/{service}/{key[:1].upper() + key[1:]}", "grpc-js", key, protocol="grpc")
    if "graphql" in text.lower() or "apollo" in text.lower() or "resolvers" in text:
        for m in _TS_RESOLVER_ROOT.finditer(text):
            for key, a, b in _object_keys(text, m.end() - 1):
                d.endpoint(d.at(a, b), m.group(1).upper(), key, "graphql-resolvers", f"{m.group(1)}.{key}", protocol="graphql", ev=EvidenceClass.E4)
    for m in _TS_NEST.finditer(text):  # NestJS / TypeGraphQL decorators
        field = m.group(2) or m.group(3)
        d.endpoint(d.at(m.start(), m.end()), m.group(1).upper(), field, "nestjs-graphql", m.group(3), protocol="graphql")


class ApiImplementations:
    id = "contract.implementations"
    version = "1.0.0"
    _SUFFIXES = (".py", ".ts", ".tsx", ".js", ".mjs", ".cjs")
    _MARKERS = (b"Servicer", b"strawberry", b"graphene", b"ariadne", b"addService", b"Query", b"Mutation", b"resolvers")

    def matches(self, path: str) -> bool:
        return path.lower().endswith(self._SUFFIXES) and not path.endswith(".d.ts")

    def extract(self, path, data, blob):
        if not any(m in data for m in self._MARKERS):
            return Extraction(parsed=False)
        text = decode(data)
        if text is None:
            return Extraction(parsed=False)
        if path.endswith(".py"):
            try:
                tree = ast.parse(text)
            except (SyntaxError, ValueError, RecursionError):
                return Extraction(parsed=False)  # python.source reports the syntax error
            d = Doc(path, data, blob, text)
            starts = [0]
            for line in data.splitlines(keepends=True):
                starts.append(starts[-1] + len(line))
            _py_impls(tree, d, starts)
        else:
            d = Doc(path, data, blob, without_comments(text, "typescript"))
            _ts_impls(d.text, d)
        return Extraction(d.out, parsed=bool(d.out))


EXTRACTORS = [Protobuf(), GraphQLSchema(), AsyncAPI(), ApiImplementations()]
