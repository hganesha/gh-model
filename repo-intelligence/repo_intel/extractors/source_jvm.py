"""Java, Kotlin and Scala source: HTTP endpoints, tables, SQL, message topics, gRPC
services and GraphQL resolvers.

Comments are blanked with Pygments, then annotations are read in groups: the run
of annotations before one declaration, with the declaration they apply to (a
class, or a method and its name).

- Endpoints: Spring MVC/WebFlux (`@RequestMapping` on the class, `@GetMapping`
  and friends or `@RequestMapping(method = ...)` on the method), JAX-RS and
  Quarkus (`@Path` with `@GET`...), Micronaut (`@Controller("/x")` with
  `@Get("/y")`) and Ktor routing blocks (`route("/x") { get("/y") { } }`). Only
  literal paths count; a computed one is counted as unresolved.
- Tables: JPA `@Entity` classes, named by `@Table(name = ...)` or, as Spring
  Boot's default naming does, by the class name in snake_case (E4 then).
- SQL: string literals given to JDBC, JdbcTemplate, `createNativeQuery`, and
  `@Query(..., nativeQuery = true)`; JPQL is not SQL and is skipped.
- Topics: `@KafkaListener(topics = ...)`, `kafkaTemplate.send("t", ...)`,
  `@SqsListener`, `@RabbitListener(queues = ...)`, `rabbitTemplate.convertAndSend`,
  `@JmsListener(destination = ...)`. `${property}` placeholders are dynamic.
- gRPC: classes extending a generated `*ImplBase` / `*CoroutineImplBase`; each
  overriding method is an RPC endpoint `/Service/Method`.
- GraphQL: Spring for GraphQL (`@QueryMapping`, `@MutationMapping`,
  `@SchemaMapping(typeName = "Query")`) and Netflix DGS (`@DgsQuery`, `@DgsData`).

Spark in Scala is read for table lineage: `spark.read.table("x")`,
`spark.table("x")`, `.saveAsTable("x")`, `.insertInto("x")` and `spark.sql("...")`.
"""

from __future__ import annotations

import posixpath
import re

from repo_intel.compile.ontology import EntityKind, EvidenceClass, Predicate
from repo_intel.extractors.base import Extraction
from repo_intel.extractors.textscan import Doc, decode, join_route, keyword_arg, looks_like_sql, matching, prefix_at, queue_name, snake, sql_in, string_args, unquote, without_comments

K = EntityKind
P = Predicate
E2, E4 = EvidenceClass.E2, EvidenceClass.E4

_LEXERS = {".java": "java", ".kt": "kotlin", ".kts": "kotlin", ".scala": "scala"}
_VERBS = {"Get": "GET", "Post": "POST", "Put": "PUT", "Delete": "DELETE", "Patch": "PATCH", "Head": "HEAD", "Options": "OPTIONS"}
_CLASS_WORDS = {"class", "interface", "object", "record", "enum"}
_MODIFIERS = {"public", "private", "protected", "internal", "static", "final", "abstract", "open", "override", "suspend", "data", "sealed", "synchronized", "default", "inline", "operator", "fun", "def", "val", "var", "lateinit", "const", "native", "transient", "volatile", "strictfp"}


class Group:
    """A run of annotations and the declaration after it."""

    def __init__(self, start: int, end: int, annotations: dict[str, tuple[str | None, int]], kind: str, name: str | None, body: tuple[int, int] | None, supertypes: str):
        self.start, self.end, self.annotations, self.kind, self.name, self.body, self.supertypes = start, end, annotations, kind, name, body, supertypes

    def args(self, name: str) -> str | None:
        found = self.annotations.get(name)
        return found[0] if found else None

    def has(self, *names: str) -> bool:
        return any(n in self.annotations for n in names)


def _declaration(text: str, i: int) -> tuple[str, str | None, tuple[int, int] | None, str]:
    """What follows an annotation group: ("class", name, body, supertypes) or ("method", name, None, "")."""
    words: list[str] = []
    while i < len(text):
        m = re.compile(r"\s*([A-Za-z_]\w*|[(){};=:<@\[]|\S)").match(text, i)
        if not m:
            break
        tok, i = m.group(1), m.end()
        if tok == "<":  # generics
            depth, j = 1, i
            while j < len(text) and depth:
                depth += {"<": 1, ">": -1}.get(text[j], 0)
                j += 1
            i = j
            continue
        if tok == "@":  # an annotation after modifiers
            a = re.compile(r"[\w.]+\s*").match(text, i)
            i = a.end() if a else i
            if i < len(text) and text[i] == "(":
                i = matching(text, i, "()")
            continue
        if tok in ("(", "{", ";", "=", "["):
            break
        if tok == ":" and any(w in _CLASS_WORDS for w in words):  # Kotlin supertypes
            break
        words.append(tok)
        if len(words) > 12:
            break
    kw = next((w for w in words if w in _CLASS_WORDS), None)
    if kw:
        name = words[words.index(kw) + 1] if words.index(kw) + 1 < len(words) else None
        brace = text.find("{", i - 1)
        semi = text.find(";", i - 1)
        if brace < 0 or (0 <= semi < brace and kw != "class"):
            return "class", name, None, ""
        return "class", name, (brace, matching(text, brace)), text[i - 1: brace]
    name = next((w for w in reversed(words) if w not in _MODIFIERS and re.fullmatch(r"[A-Za-z_]\w*", w)), None)
    return "method", name, None, ""


def annotation_groups(text: str) -> list[Group]:
    out: list[Group] = []
    i = 0
    rx = re.compile(r"@([A-Za-z_][\w.]*)")
    while True:
        m = rx.search(text, i)
        if not m:
            return out
        start, annotations = m.start(), {}
        j = m.start()
        while True:
            a = rx.match(text, j)
            if not a:
                break
            name, j = a.group(1).rsplit(".", 1)[-1], a.end()
            k = j
            while k < len(text) and text[k] in " \t":
                k += 1
            args = None
            if k < len(text) and text[k] == "(":
                close = matching(text, k, "()")
                args, j = text[k + 1: close - 1], close
            annotations.setdefault(name, (args, a.start()))
            s = re.compile(r"\s*").match(text, j)
            if s and s.end() < len(text) and text[s.end()] == "@" and not text.startswith("@interface", s.end()):
                j = s.end()
                continue
            break
        kind, name, body, supertypes = _declaration(text, j)
        out.append(Group(start, j, annotations, kind, name, body, supertypes))
        i = j


def _paths(args: str | None, *keys: str) -> list[str] | None:
    """Literal paths of a mapping annotation; [] when it has none, None when computed."""
    if args is None or not args.strip():
        return [""]
    raw = keyword_arg(args, *keys) if re.search(rf"(?<![\w.])({'|'.join(keys)})\s*=", args) else args if "=" not in args.split(",", 1)[0] else None
    if raw is None:
        return [""]
    found = [v for v, _, _ in string_args(raw)]
    rest = re.sub(r'"(?:[^"\\\n]|\\.)*"', "", raw)
    if (not found and raw.strip()) or re.search(r"[A-Za-z_+]", rest):
        return None  # a constant or an expression: not a literal path
    return found


def _topic_args(raw: str | None, consts: dict[str, str]) -> list[tuple[str | None, str | None]]:
    """(topic, constant name) for each topic a listener names; (None, None) if computed."""
    if raw is None:
        return []
    out = []
    for v, _, _ in string_args(raw):
        out.append((None, None) if "${" in v or "#{" in v else (queue_name(v), None))
    if not out and ('"' in raw or "'" in raw):
        return [(None, None)]  # a placeholder or an expression: known only at run time
    if not out:
        for name in re.findall(r"[A-Za-z_][\w.]*", raw):
            if name in consts:
                out.append((queue_name(consts[name]), None))
            elif name[:1].isupper() or "." in name:
                out.append((None, name))
    return out or [(None, None)]


_CONST = re.compile(r"""(?:static\s+final\s+String|const\s+val|final\s+val|val|String)\s+([A-Z_][A-Z0-9_]*)\s*(?::\s*String\s*)?=\s*("(?:[^"\\\n]|\\.)*")""")
_SQL_CALL = re.compile(r"""\.(?:query|queryForObject|queryForList|queryForMap|queryForRowSet|update|batchUpdate|execute|prepareStatement|prepareCall|executeQuery|executeUpdate|createNativeQuery)\s*\(\s*(\"\"\"(?:.|\n)*?\"\"\"|"(?:[^"\\\n]|\\.)*")""")
_KAFKA_SEND = re.compile(r"""\b(\w*[kK]afka\w*|\w*[tT]emplate)\s*\.\s*(send|sendDefault)\s*\(\s*("(?:[^"\\\n]|\\.)*"|[A-Za-z_][\w.]*)""")
_SQS_SEND = re.compile(r"""\b(\w*[sS]qs\w*)\s*\.\s*(send|convertAndSend)\s*\(\s*("(?:[^"\\\n]|\\.)*"|[A-Za-z_][\w.]*)""")
_RABBIT_SEND = re.compile(r"""\b(\w*[rR]abbit\w*|\w*[aA]mqp\w*)\s*\.\s*(convertAndSend|send)\s*\(\s*("(?:[^"\\\n]|\\.)*"|[A-Za-z_][\w.]*)""")
_GRPC_CLASS = re.compile(r"""\bclass\s+(\w+)[^{;]*?\b(\w+)Grpc(?:Kt)?\s*\.\s*(\w+?)(?:Coroutine)?ImplBase\b[^{;]*\{""")
_KTOR_ROUTE = re.compile(r"""\broute\s*\(\s*"([^"\n]*)"\s*\)\s*\{""")
_KTOR_VERB = re.compile(r"""\b(get|post|put|delete|patch|head|options)\s*(?:\(\s*"([^"\n]*)"\s*\))?\s*\{""")
_SPARK_READ = re.compile(r"""\b(?:read\s*\.\s*table|\.table)\s*\(\s*"([\w.`]+)"\s*\)""")
_SPARK_WRITE = re.compile(r"""\.(?:saveAsTable|insertInto)\s*\(\s*"([\w.`]+)"\s*\)""")
_SPARK_SQL = re.compile(r"""\bspark\s*\.\s*sql\s*\(\s*(\"\"\"(?:.|\n)*?\"\"\"|"(?:[^"\\\n]|\\.)*")""")
_MARKERS = (b"Mapping", b"@Path", b"@Get", b"@Post", b"@Controller", b"@Entity", b"Listener", b"Template", b"ImplBase", b"@Query", b"@Dgs", b"@Schema", b"io.ktor", b"jdbc", b"Jdbc", b"createNativeQuery", b"spark", b"KafkaProducer", b"executeQuery", b"prepareStatement")


def _literal(raw: str) -> str | None:
    if raw.startswith('"""'):
        return raw[3:-3]
    return unquote(raw)


class JvmSource:
    id = "jvm.source"
    version = "1.0.0"

    def matches(self, path: str) -> bool:
        return posixpath.splitext(path)[1].lower() in _LEXERS and not posixpath.basename(path).endswith(".gradle.kts")

    def extract(self, path, data, blob):
        if not any(m in data for m in _MARKERS):
            return Extraction(parsed=False)
        raw = decode(data)
        if raw is None:
            return Extraction(parsed=False)
        ext = posixpath.splitext(path)[1].lower()
        text = without_comments(raw, _LEXERS[ext])
        d = Doc(path, data, blob, text)
        consts = {m.group(1): v for m in _CONST.finditer(text) if (v := unquote(m.group(2))) is not None}
        groups = annotation_groups(text)
        classes = [g for g in groups if g.kind == "class" and g.body]
        micronaut = "io.micronaut" in text
        unresolved_routes = 0

        def owner(pos: int) -> Group | None:
            inside = [g for g in classes if g.body[0] < pos < g.body[1]]
            return max(inside, key=lambda g: g.body[0]) if inside else None

        def class_prefix(cls: Group | None) -> list[str]:
            if cls is None:
                return [""]
            if cls.has("RequestMapping"):
                return _paths(cls.args("RequestMapping"), "value", "path") or [""]
            if cls.has("Path"):
                return _paths(cls.args("Path"), "value") or [""]
            if micronaut and cls.has("Controller"):
                return _paths(cls.args("Controller"), "value") or [""]
            return [""]

        for g in groups:
            if g.kind != "method":
                continue
            cls = owner(g.start)
            at = d.at(g.start, g.end)
            handler = f"{cls.name}.{g.name}" if cls and g.name else g.name
            # Spring MVC / WebFlux
            mapping = next((a for a in g.annotations if a.endswith("Mapping") and a[:-7] in _VERBS), None)
            if mapping or "RequestMapping" in g.annotations:
                if mapping:
                    methods, args = [_VERBS[mapping[:-7]]], g.args(mapping)
                else:
                    args = g.args("RequestMapping")
                    spec = keyword_arg(args or "", "method") or ""
                    methods = sorted(set(re.findall(r"\b(GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)\b", spec))) or ["ANY"]
                paths = _paths(args, "value", "path")
                if paths is None:
                    unresolved_routes += 1
                    continue
                for prefix in class_prefix(cls):
                    for p in paths or [""]:
                        for method in methods:
                            d.endpoint(at, method, join_route(prefix, p), "spring", handler)
                continue
            # JAX-RS / Quarkus
            verb = next((a for a in g.annotations if a in ("GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS")), None)
            if verb:
                paths = _paths(g.args("Path"), "value") if g.has("Path") else [""]
                if paths is None:
                    unresolved_routes += 1
                    continue
                for prefix in class_prefix(cls):
                    for p in paths or [""]:
                        d.endpoint(at, verb, join_route(prefix, p), "jaxrs", handler)
                continue
            # Micronaut
            verb = next((a for a in g.annotations if a in _VERBS), None)
            if verb and micronaut:
                paths = _paths(g.args(verb), "value", "uri")
                if paths is None:
                    unresolved_routes += 1
                    continue
                for prefix in class_prefix(cls):
                    for p in paths or [""]:
                        d.endpoint(at, _VERBS[verb], join_route(prefix, p), "micronaut", handler)
                continue
            # GraphQL resolvers
            for ann, op in (("QueryMapping", "QUERY"), ("MutationMapping", "MUTATION"), ("SubscriptionMapping", "SUBSCRIPTION"), ("DgsQuery", "QUERY"), ("DgsMutation", "MUTATION"), ("DgsSubscription", "SUBSCRIPTION")):
                if g.has(ann):
                    args = g.args(ann) or ""
                    field = (string_args(keyword_arg(args, "name", "field") or args) or [(g.name, 0, 0)])[0][0]
                    if field:
                        d.endpoint(at, op, field, "spring-graphql" if ann.endswith("Mapping") else "dgs", handler, protocol="graphql")
            for ann, type_key, field_key in (("SchemaMapping", "typeName", "field"), ("DgsData", "parentType", "field")):
                args = g.args(ann)
                if args is not None:
                    parent = (string_args(keyword_arg(args, type_key) or "") or [(None,)])[0][0]
                    field = (string_args(keyword_arg(args, field_key, "value") or "") or [(g.name,)])[0][0]
                    if parent in ("Query", "Mutation", "Subscription") and field:
                        d.endpoint(at, parent.upper(), field, "spring-graphql" if ann == "SchemaMapping" else "dgs", handler, protocol="graphql")
            # Message listeners
            for ann, keys, transport in (("KafkaListener", ("topics", "value"), "kafka"), ("SqsListener", ("value", "queueNames"), "sqs"), ("RabbitListener", ("queues", "value"), "rabbitmq"), ("JmsListener", ("destination",), "jms")):
                if g.has(ann):
                    args = g.args(ann) or ""
                    raw_topics = keyword_arg(args, *keys) if re.search(rf"(?<![\w.])({'|'.join(keys)})\s*=", args) else args
                    for topic, const in _topic_args(raw_topics, consts):
                        d.topic(at, "subscribe", transport, topic, const)
            # Native SQL in Spring Data
            args = g.args("Query")
            if args is not None and re.search(r"nativeQuery\s*=\s*true", args):
                lit = next(iter(re.finditer(r'\"\"\"(?:.|\n)*?\"\"\"|"(?:[^"\\\n]|\\.)*"', keyword_arg(args, "value") or args)), None)
                if lit:
                    offset = text.find(lit.group(), g.start)
                    sql = _literal(lit.group())
                    if sql and offset >= 0:
                        sql_in(d, sql, offset + (3 if lit.group().startswith('"""') else 1))

        # JPA entities (a Kotlin entity may have no body)
        for g in (g for g in groups if g.kind == "class"):
            if g.has("Entity"):
                named = string_args(keyword_arg(g.args("Table") or "", "name") or "") if g.has("Table") else []
                at = d.at(g.start, g.end)
                if named:
                    schema = string_args(keyword_arg(g.args("Table") or "", "schema") or "")
                    name = f"{schema[0][0]}.{named[0][0]}" if schema else named[0][0]
                    d.table(at, name.lower(), None, {"orm": "jpa", "model": g.name})
                elif g.name:
                    d.table(at, snake(g.name), None, {"orm": "jpa", "model": g.name, "name_from": "default_naming"}, E4)

        # gRPC services
        for cm in _GRPC_CLASS.finditer(text):
            service, cls_name = cm.group(3), cm.group(1)
            start = cm.end() - 1
            body = text[start: matching(text, start)]
            for fm in re.finditer(r"(?:public\s+void\s+|override\s+(?:suspend\s+)?fun\s+)(\w+)\s*\(", body):
                depth = body[: fm.start()].count("{") - body[: fm.start()].count("}")
                if depth == 1:
                    name = fm.group(1)
                    a = start + fm.start(1)
                    d.endpoint(d.at(a, a + len(name)), "RPC", f"/{service}/{name[:1].upper() + name[1:]}", "grpc-java", f"{cls_name}.{name}", protocol="grpc")

        # Ktor routing
        if "io.ktor" in text:
            blocks = [(m.end() - 1, matching(text, m.end() - 1), m.group(1)) for m in _KTOR_ROUTE.finditer(text)]
            for m in _KTOR_VERB.finditer(text):
                if not re.search(r"(routing|route|Route|install)\b", text[: m.start()]):
                    continue
                route = join_route(prefix_at(m.start(), blocks), m.group(2) or "")
                d.endpoint(d.at(m.start(), m.end() - 1), m.group(1).upper(), route, "ktor")

        # SQL given to JDBC and friends
        for m in _SQL_CALL.finditer(text):
            sql = _literal(m.group(1))
            if sql and looks_like_sql(sql):
                sql_in(d, sql, m.start(1) + (3 if m.group(1).startswith('"""') else 1))

        # Producers
        for rx, transport in ((_KAFKA_SEND, "kafka"), (_SQS_SEND, "sqs"), (_RABBIT_SEND, "rabbitmq")):
            for m in rx.finditer(text):
                if transport == "kafka" and "kafka" not in text.lower():
                    continue
                arg = m.group(3)
                lit = unquote(arg)
                if lit is not None:
                    d.topic(d.at(m.start(), m.end()), "publish", transport, None if "${" in lit else queue_name(lit))
                elif arg in consts:
                    d.topic(d.at(m.start(), m.end()), "publish", transport, queue_name(consts[arg]))
                else:
                    d.topic(d.at(m.start(), m.end()), "publish", transport, None, arg if re.fullmatch(r"[A-Z_][\w.]*|\w+\.[A-Z_]\w*", arg) else None)

        # Spark (Scala and Java)
        if "spark" in text:
            for rx, pred in ((_SPARK_READ, P.READS_FROM), (_SPARK_WRITE, P.WRITES_TO)):
                for m in rx.finditer(text):
                    d.resolved += 1
                    d.table(d.at(m.start(1), m.end(1)), m.group(1).replace("`", "").lower(), pred)
            for m in _SPARK_SQL.finditer(text):
                sql = _literal(m.group(1))
                if sql:
                    sql_in(d, sql, m.start(1) + (3 if m.group(1).startswith('"""') else 1), "spark")
                else:
                    d.unresolved += 1

        if unresolved_routes:
            d.observe("route_summary", d.whole(), {"unresolved": unresolved_routes})
        if not d.out:
            return Extraction(parsed=False)
        return d.done() if d.resolved or d.unresolved else Extraction(d.out)


_PLAY_ROUTE = re.compile(r"(?m)^[ \t]*(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS|\*)[ \t]+(/\S*)[ \t]+([\w.@]+)")


class PlayRoutes:
    """Play Framework `conf/routes`: `GET /orders/:id controllers.Orders.show(id: Long)`.
    `->` includes of other routers are not followed."""

    id = "jvm.play_routes"
    version = "1.0.0"

    def matches(self, path: str) -> bool:
        return path == "conf/routes" or path.endswith("/conf/routes") or path.endswith(".routes") and "/conf/" in f"/{path}"

    def extract(self, path, data, blob):
        raw = decode(data)
        if raw is None:
            return Extraction(parsed=False)
        text = re.sub(r"(?m)#[^\n]*", lambda m: " " * len(m.group()), raw)
        d = Doc(path, data, blob, text)
        for m in _PLAY_ROUTE.finditer(text):
            d.endpoint(d.at(m.start(), m.end()), "ANY" if m.group(1) == "*" else m.group(1), m.group(2), "play", m.group(3))
        return Extraction(d.out) if d.out else Extraction(parsed=False)


EXTRACTORS = [JvmSource(), PlayRoutes()]
