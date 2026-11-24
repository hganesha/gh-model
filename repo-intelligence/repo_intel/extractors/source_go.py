"""Go source: HTTP routes, SQL, Kafka topics and gRPC services.

Routes are read from net/http (`mux.HandleFunc("/x", h)`, and Go 1.22 patterns
such as `"GET /orders/{id}"`), gorilla/mux (`.Methods("GET")`, `PathPrefix(...).
Subrouter()`), gin, echo and fiber (`r.GET`, `app.Get`, `g := r.Group("/api")`)
and chi (`r.Get`, `r.Route("/api", func(r chi.Router) { ... })`). A group
assigned to a variable carries its prefix to the routes registered on it; a
chi `Route` block carries it to the routes inside. A route registration must
name a literal path that starts with "/" and a handler, so `cfg.Get("key")` is
never read as a route.

SQL is read from string literals (including raw `backtick` strings) given to
database/sql, sqlx and pgx (`Query`, `QueryRow`, `Exec`, their `Context`
variants, `Get`, `Select`); GORM tables from `db.Table("x")` and `TableName()`
methods. Kafka: segmentio/kafka-go writers and readers, Sarama messages and
consumers, confluent-kafka-go subscriptions. gRPC: a struct embedding
`Unimplemented<Service>Server` implements `<Service>`; its exported methods are
the RPCs.
"""

from __future__ import annotations

import re

from repo_intel.compile.ontology import EntityKind, Predicate
from repo_intel.extractors.base import Extraction
from repo_intel.extractors.textscan import Doc, decode, join_route, looks_like_sql, matching, prefix_at, queue_name, sql_in, unquote, without_comments

K = EntityKind
P = Predicate

_STR = r'(`[^`]*`|"(?:[^"\\\n]|\\.)*")'
_IMPORTS = {"net/http": "net/http", "github.com/gin-gonic/gin": "gin", "github.com/labstack/echo": "echo", "github.com/gofiber/fiber": "fiber", "github.com/go-chi/chi": "chi", "github.com/gorilla/mux": "gorilla"}
_GROUP = re.compile(r"""\b(\w+)\s*:?=\s*(\w+)\s*\.\s*(?:Group|PathPrefix|Route)\(\s*"([^"\n]*)"\s*(?:\)\s*(?:\.\s*Subrouter\(\s*\))?|,)""")
_VERB = re.compile(r"""\b(\w+)\s*\.\s*(GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS|Any|All|Get|Post|Put|Delete|Patch|Head|Options|HandleFunc|Handle)\(\s*"([^"\n]*)"\s*,\s*([\w.&]+)?""")
_CHI_METHOD = re.compile(r"""\b(\w+)\s*\.\s*(?:Method|MethodFunc)\(\s*(?:http\.Method(\w+)|"(\w+)")\s*,\s*"([^"\n]*)"\s*,\s*([\w.&]+)?""")
_CHI_ROUTE = re.compile(r"""\b\w+\s*\.\s*Route\(\s*"([^"\n]*)"\s*,\s*func\s*\([^)]*\)\s*\{""")
_METHODS_CHAIN = re.compile(r"""\s*\)?\s*\.\s*Methods\(([^)]*)\)""")
_SQL_CALL = re.compile(rf"""\.\s*(?:Query|QueryRow|Exec|QueryContext|QueryRowContext|ExecContext|Get|Select|GetContext|SelectContext|NamedExec|NamedQuery|Prepare|PrepareContext|Raw)\(\s*(?:[\w.&]+\s*,\s*){{0,2}}{_STR}""")
_GORM_TABLE = re.compile(r"""\.\s*Table\(\s*"([\w.]+)"\s*\)""")
_TABLE_NAME = re.compile(r"""func\s*\(\s*\w*\s*\*?(\w+)\s*\)\s*TableName\(\s*\)\s*string\s*\{\s*return\s+"([\w.]+)\"""")
_CONST = re.compile(r"""(?m)^\s*(?:const\s+|var\s+)?(\w+)\s*(?:string\s*)?:?=\s*"([^"\n]*)"\s*$""")
_KAFKA = [
    (re.compile(rf"""kafka\s*\.\s*Writer\s*\{{[^}}]*?\bTopic\s*:\s*({_STR}|[\w.]+)"""), "publish"),
    (re.compile(rf"""kafka\s*\.\s*ReaderConfig\s*\{{[^}}]*?\bTopic\s*:\s*({_STR}|[\w.]+)"""), "subscribe"),
    (re.compile(rf"""sarama\s*\.\s*ProducerMessage\s*\{{[^}}]*?\bTopic\s*:\s*({_STR}|[\w.]+)"""), "publish"),
    (re.compile(rf"""\.\s*ConsumePartition\(\s*({_STR}|[\w.]+)"""), "subscribe"),
    (re.compile(rf"""\.\s*(?:SubscribeTopics|Consume)\(\s*(?:ctx\w*\s*,\s*)?\[\]string\s*\{{\s*({_STR}|[\w.]+)"""), "subscribe"),
    (re.compile(rf"""\.\s*Subscribe\(\s*({_STR}|[\w.]+)\s*,\s*nil"""), "subscribe"),
]
_GRPC_STRUCT = re.compile(r"""type\s+(\w+)\s+struct\s*\{[^}]*?\b(?:\w+\.)?Unimplemented(\w+)Server\b""")


class GoSource:
    id = "go.source"
    version = "1.0.0"

    def matches(self, path: str) -> bool:
        return path.endswith(".go") and not path.endswith(".pb.go") and not path.endswith("_grpc.pb.go")

    def extract(self, path, data, blob):
        raw = decode(data)
        if raw is None:
            return Extraction(parsed=False)
        imports = {fw for prefix, fw in _IMPORTS.items() if f'"{prefix}' in raw}
        has_sql = any(m in raw for m in ('"database/sql"', "sqlx", "pgx", "gorm.io", "TableName()"))
        has_kafka = any(m in raw for m in ("kafka-go", "sarama", "confluent-kafka-go"))
        has_grpc = "Unimplemented" in raw
        if not (imports or has_sql or has_kafka or has_grpc):
            return Extraction(parsed=False)
        text = without_comments(raw, "go")
        d = Doc(path, data, blob, text)
        consts = {m.group(1): m.group(2) for m in _CONST.finditer(text)}
        if imports:
            self._routes(d, text, imports)
        if has_sql:
            for m in _SQL_CALL.finditer(text):
                sql = unquote(m.group(1))
                if sql and looks_like_sql(sql):
                    sql_in(d, sql, m.start(1) + 1, "postgres" if "pgx" in raw or "lib/pq" in raw else None)
            for m in _GORM_TABLE.finditer(text):
                d.table(d.at(m.start(1), m.end(1)), m.group(1).lower(), None, {"orm": "gorm"})
            for m in _TABLE_NAME.finditer(text):
                d.table(d.at(m.start(2), m.end(2)), m.group(2).lower(), None, {"orm": "gorm", "model": m.group(1)})
        if has_kafka:
            for rx, role in _KAFKA:
                for m in rx.finditer(text):
                    arg = m.group(1)
                    lit = unquote(arg)
                    topic = queue_name(lit) if lit is not None else queue_name(consts[arg]) if arg in consts else None
                    d.topic(d.at(m.start(), m.end()), role, "kafka", topic, arg if lit is None and topic is None and re.fullmatch(r"[A-Za-z_][\w.]*", arg) else None)
        if has_grpc:
            for m in _GRPC_STRUCT.finditer(text):
                receiver, service = m.group(1), m.group(2)
                for fm in re.finditer(rf"""func\s*\(\s*\w*\s*\*?{receiver}\s*\)\s*([A-Z]\w*)\s*\(""", text):
                    d.endpoint(d.at(fm.start(1), fm.end(1)), "RPC", f"/{service}/{fm.group(1)}", "grpc-go", f"{receiver}.{fm.group(1)}", protocol="grpc")
        if not d.out:
            return Extraction(parsed=False)
        return d.done() if d.resolved or d.unresolved else Extraction(d.out)

    def _routes(self, d: Doc, text: str, imports: set[str]) -> None:
        prefixes: dict[str, str] = {}
        for m in _GROUP.finditer(text):  # in source order, so a group of a group resolves
            prefixes[m.group(1)] = join_route(prefixes.get(m.group(2), ""), m.group(3))
        blocks = [(m.end() - 1, matching(text, m.end() - 1), m.group(1)) for m in _CHI_ROUTE.finditer(text)]
        framework = sorted(imports - {"net/http"})[0] if imports - {"net/http"} else "net/http"
        for m in _VERB.finditer(text):
            var, verb, route, handler = m.group(1), m.group(2), m.group(3), m.group(4)
            if not route.startswith("/") and not re.match(r"^[A-Z]+ /", route):
                continue
            if verb in ("HandleFunc", "Handle"):
                method, _, pattern = route.partition(" ") if re.match(r"^[A-Z]+ /", route) else ("ANY", "", route)
                route = pattern
                chain = _METHODS_CHAIN.match(text, matching(text, text.index("(", m.start(2)), "()") - 1)
                methods = re.findall(r'"([A-Z]+)"|http\.Method(\w+)', chain.group(1)) if chain else []
                methods = [(a or b).upper() for a, b in methods] or [method]
            else:
                methods = ["ANY" if verb in ("Any", "All") else verb.upper()]
            full = join_route(prefix_at(m.start(), blocks), prefixes.get(var, ""), route)
            for method in methods:
                d.endpoint(d.at(m.start(), m.end()), method, full, framework, handler)
        for m in _CHI_METHOD.finditer(text):
            method = (m.group(2) or m.group(3)).upper()
            d.endpoint(d.at(m.start(), m.end()), method, join_route(prefix_at(m.start(), blocks), prefixes.get(m.group(1), ""), m.group(4)), "chi", m.group(5))


EXTRACTORS = [GoSource()]
