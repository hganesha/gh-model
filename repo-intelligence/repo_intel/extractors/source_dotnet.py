"""C# source: ASP.NET Core endpoints, EF Core tables, SQL, gRPC services and
message topics.

- Controllers: `[Route("api/[controller]")]` on the class and `[HttpGet("{id}")]`
  (or `[Route(...)]` with `[HttpPost]`) on actions; `[controller]` and `[action]`
  are replaced as ASP.NET Core does (the class name without "Controller", the
  method name). A template starting with "/" or "~/" ignores the class route.
- Minimal APIs: `app.MapGet("/x", ...)` and friends, with `var g =
  app.MapGroup("/api")` prefixes carried to the routes mapped on `g`.
- EF Core: each `DbSet<T> Name` is a table named `Name` (EF's convention), unless
  `[Table("x")]` on T or `.Entity<T>().ToTable("x")` in the same file names it.
- SQL: literals given to `FromSqlRaw`, `ExecuteSqlRaw`, Dapper's `Query*` and
  `Execute*`, and `new SqlCommand(...)`; interpolated strings with holes are not
  read.
- gRPC: a class deriving from `<Service>.<Service>Base`; its `override` methods
  are RPCs `/Service/Method`.
- Topics: Confluent.Kafka `ProduceAsync("t", ...)` / `Produce` / `Subscribe("t")`,
  Azure Service Bus `CreateSender("q")` / `CreateReceiver` / `CreateProcessor`.
"""

from __future__ import annotations

import re

from repo_intel.compile.ontology import EntityKind
from repo_intel.extractors.base import Extraction
from repo_intel.extractors.textscan import Doc, code_traits, decode, join_route, keyword_arg, looks_like_sql, matching, queue_name, sql_in, string_args, unquote, without_comments

K = EntityKind

_STR = r'("""(?:.|\n)*?"""|@"(?:[^"]|"")*"|"(?:[^"\\\n]|\\.)*")'
_HTTP = {"HttpGet": "GET", "HttpPost": "POST", "HttpPut": "PUT", "HttpDelete": "DELETE", "HttpPatch": "PATCH", "HttpHead": "HEAD", "HttpOptions": "OPTIONS"}
_ATTR_LIST = re.compile(r"(?m)^[ \t]*\[(?!\s*\])")
_ATTR = re.compile(r"\s*(\w+)(?:Attribute)?\s*(\(([^()]*(?:\([^()]*\)[^()]*)*)\))?\s*,?")
_MAP = re.compile(r"""\b(\w+)\s*\.\s*Map(Get|Post|Put|Delete|Patch|Methods)\(\s*"([^"\n]*)"\s*(?:,\s*new\s*\[\]\s*\{([^}]*)\})?""")
_MAP_GROUP = re.compile(r"""\b(?:var\s+)?(\w+)\s*=\s*(\w+)\s*\.\s*MapGroup\(\s*"([^"\n]*)"\s*\)""")
_DBSET = re.compile(r"""\bDbSet<\s*(\w+)\s*>\s+(\w+)\s*(?:\{|=>)""")
_TO_TABLE = re.compile(r"""Entity<\s*(\w+)\s*>\s*\(\s*\)\s*\.\s*ToTable\(\s*"([\w.]+)"(?:\s*,\s*"(\w+)")?""")
_SQL_CALL = re.compile(rf"""(?:\.\s*(?:FromSqlRaw|ExecuteSqlRaw|ExecuteSqlRawAsync|SqlQueryRaw|Query|QueryAsync|QueryFirst|QueryFirstAsync|QueryFirstOrDefault|QueryFirstOrDefaultAsync|QuerySingle|QuerySingleAsync|QuerySingleOrDefault|QuerySingleOrDefaultAsync|QueryMultiple|QueryMultipleAsync|Execute|ExecuteAsync|ExecuteScalar|ExecuteScalarAsync)(?:<[^>()]*>)?|new\s+(?:Sql|Npgsql|MySql|Sqlite)Command)\(\s*{_STR}""")
_GRPC = re.compile(r"""\bclass\s+(\w+)\s*:\s*(\w+)\s*\.\s*(\w+)Base\b[^{]*\{""")
_TOPICS = [
    (re.compile(rf"""\.\s*(?:ProduceAsync|Produce)\(\s*({_STR}|[\w.]+)"""), "publish", "kafka"),
    (re.compile(rf"""\.\s*Subscribe\(\s*({_STR}|[\w.]+)\s*\)"""), "subscribe", "kafka"),
    (re.compile(rf"""\.\s*CreateSender\(\s*({_STR}|[\w.]+)"""), "publish", "servicebus"),
    (re.compile(rf"""\.\s*(?:CreateReceiver|CreateProcessor|CreateSessionProcessor)\(\s*({_STR}|[\w.]+)"""), "subscribe", "servicebus"),
]
_CONST = re.compile(r"""\bconst\s+string\s+(\w+)\s*=\s*"([^"\n]*)"\s*;""")


def _lit(raw: str) -> str | None:
    if raw.startswith('"""'):
        return raw[3:-3]
    if raw.startswith('@"'):
        return raw[2:-1].replace('""', '"')
    return unquote(raw)


def _groups(text: str) -> list[tuple[int, int, dict[str, str | None], str, str | None, tuple[int, int] | None]]:
    """(start, end, attributes, kind, name, body) for each run of attribute lists."""
    out = []
    i = 0
    while True:
        m = _ATTR_LIST.search(text, i)
        if not m:
            return out
        start, attrs, j = m.end() - 1, {}, m.end() - 1
        while j < len(text) and text[j] == "[":
            close = matching(text, j, "[]")
            inner = text[j + 1: close - 1]
            for a in _ATTR.finditer(inner):
                if a.group(1):
                    attrs.setdefault(a.group(1), a.group(3) if a.group(2) else None)
            j = close
            while j < len(text) and text[j] in " \t\r\n":
                j += 1
        head = re.match(r"[^;{(=]*", text[j:]).group()
        if re.search(r"\b(class|record|struct|interface)\s+(\w+)", head):
            name = re.search(r"\b(class|record|struct|interface)\s+(\w+)", head).group(2)
            brace = text.find("{", j)
            out.append((start, j, attrs, "class", name, (brace, matching(text, brace)) if brace >= 0 else None))
        else:
            names = re.findall(r"\b([A-Za-z_]\w*)\s*(?:<[^<>]*>)?\s*$", head)
            out.append((start, j, attrs, "method", names[0] if names else None, None))
        i = max(j, m.end())


_RESILIENCE_MARKERS = ("Polly", "Resilience", "RateLimit", "Timeout", "CancelAfter", "SemaphoreSlim", "deadline:")


def _dotnet_rules(raw: str, text: str) -> list:
    """Resilience policies (spec §8.8) in C#: Polly v7 policies and v8 pipelines,
    Microsoft.Extensions.Http.Resilience, ASP.NET Core rate limiting, HttpClient and
    cancellation timeouts, gRPC deadlines and bounded SemaphoreSlims."""
    polly = "Polly" in raw or "Resilience" in raw
    rules = []
    if polly:
        rules += [
            (re.compile(r"\b(?:Advanced)?CircuitBreaker(?:Async)?\s*(?:<[^>]*>)?\s*\(|\.AddCircuitBreaker\s*\("), {"trait": "circuit_breaker", "library": "polly"}, "in"),
            (re.compile(r"\bBulkhead(?:Async)?\s*(?:<[^>]*>)?\s*\(|\.AddConcurrencyLimiter\s*\("), {"trait": "bulkhead", "library": "polly"}, "in"),
            (re.compile(r"\bPolicy\s*\.\s*Timeout(?:Async)?\s*(?:<[^>]*>)?\s*\(|\.AddTimeout\s*\("), {"trait": "timeout", "construct": "polly", "deadline": False}, "in"),
            (re.compile(r"\.AddStandardResilienceHandler\s*\("), [{"trait": "circuit_breaker", "library": "Microsoft.Extensions.Http.Resilience"}, {"trait": "timeout", "construct": "standard resilience handler", "deadline": False}], "in"),
        ]
    rules += [
        (re.compile(r"\.AddRateLimiter\s*\("), {"trait": "rate_limit", "library": "polly" if polly and "AspNetCore.RateLimiting" not in raw else "aspnetcore", **({} if polly and "AspNetCore.RateLimiting" not in raw else {"direction": "inbound"})}, "in"),
        (re.compile(r"\[EnableRateLimiting\b[^\]]*\]"), {"trait": "rate_limit", "library": "aspnetcore", "direction": "inbound"}, "on"),
        (re.compile(r"\.RequireRateLimiting\s*\("), {"trait": "rate_limit", "library": "aspnetcore", "direction": "inbound"}, "in"),
        (re.compile(r"\.Timeout\s*=\s*TimeSpan\.|\.CancelAfter\s*\(|new\s+CancellationTokenSource\s*\(\s*TimeSpan\."), lambda m: {"trait": "timeout", "construct": "HttpClient.Timeout" if "Timeout" in m.group(0) else "CancelAfter", "deadline": False}, "in"),
        (re.compile(r"\bdeadline\s*:\s*DateTime"), {"trait": "timeout", "construct": "grpc deadline", "deadline": True}, "in"),
        (re.compile(r"\bnew\s+SemaphoreSlim\s*\(\s*\d+"), lambda m: {"trait": "concurrency_limit", "name": (re.search(r"(\w+)\s*=\s*$", text[max(0, m.start() - 120):m.start()]) or [None, "SemaphoreSlim"])[1], "construct": "SemaphoreSlim"}, "in"),
    ]
    return rules


class DotnetSource:
    id = "dotnet.source"
    version = "1.1.0"

    def matches(self, path: str) -> bool:
        return path.endswith(".cs") and not path.endswith((".Designer.cs", ".g.cs"))

    def extract(self, path, data, blob):
        raw = decode(data)
        if raw is None or not any(m in raw for m in ("Http", "Map", "DbSet", "Sql", "Query", "Execute", "Base", "Produce", "Subscribe", "CreateSender", "CreateReceiver", "CreateProcessor", "[Table", *_RESILIENCE_MARKERS)):
            return Extraction(parsed=False)
        text = without_comments(raw, "csharp")
        d = Doc(path, data, blob, text)
        consts = {m.group(1): m.group(2) for m in _CONST.finditer(text)}
        groups = _groups(text)
        classes = [g for g in groups if g[3] == "class" and g[5]]

        # Controllers
        for start, end, attrs, kind, name, _ in groups:
            if kind != "method":
                continue
            verbs = [v for a, v in _HTTP.items() if a in attrs]
            if not verbs and not (attrs.keys() & {"Route", "AcceptVerbs"}):
                continue
            owner = max((c for c in classes if c[5][0] < start < c[5][1]), key=lambda c: c[5][0], default=None)
            if owner is None or not (owner[4] or "").endswith("Controller") and not owner[2].keys() & {"ApiController", "Route"}:
                continue
            controller = (owner[4] or "")[: -len("Controller")] if (owner[4] or "").endswith("Controller") else owner[4] or ""
            class_routes = [v for v, *_ in string_args(owner[2].get("Route") or "")] or [""]
            templates = []
            for a in [*(x for x in _HTTP if x in attrs), "Route"]:
                args = attrs.get(a) or ""
                templates += [v for v, *_ in string_args(keyword_arg(args, "template") or args.split(",", 1)[0])]
            templates = templates or [""]
            methods = verbs or ["ANY"]
            for base in class_routes:
                for t in templates:
                    route = t[1:].lstrip("/") if t.startswith(("/", "~/")) else join_route(base, t)
                    route = route.replace("[controller]", controller).replace("[action]", name or "")
                    for method in methods:
                        d.endpoint(d.at(start, end), method, join_route(route), "aspnetcore", f"{owner[4]}.{name}")

        # Minimal APIs
        prefixes: dict[str, str] = {}
        for m in _MAP_GROUP.finditer(text):
            prefixes[m.group(1)] = join_route(prefixes.get(m.group(2), ""), m.group(3))
        for m in _MAP.finditer(text):
            methods = [v for v, *_ in string_args(m.group(4) or "")] if m.group(2) == "Methods" else [m.group(2).upper()]
            for method in methods or ["ANY"]:
                d.endpoint(d.at(m.start(), m.end()), method.upper(), join_route(prefixes.get(m.group(1), ""), m.group(3)), "aspnetcore-minimal")

        # EF Core
        named = {m.group(1): (f"{m.group(3)}.{m.group(2)}" if m.group(3) else m.group(2)) for m in _TO_TABLE.finditer(text)}
        for start, end, attrs, kind, name, _ in groups:
            if kind == "class" and "Table" in attrs and name:
                found = string_args(attrs["Table"] or "")
                if found:
                    schema = string_args(keyword_arg(attrs["Table"] or "", "Schema") or "")
                    named.setdefault(name, f"{schema[0][0]}.{found[0][0]}" if schema else found[0][0])
                    d.table(d.at(start, end), named[name].lower(), None, {"orm": "efcore", "model": name})
        for m in _DBSET.finditer(text):
            entity, prop = m.group(1), m.group(2)
            d.table(d.at(m.start(), m.end()), named.get(entity, prop).lower(), None, {"orm": "efcore", "model": entity, **({} if entity in named else {"name_from": "dbset_property"})})

        # SQL
        for m in _SQL_CALL.finditer(text):
            sql = _lit(m.group(1))
            if sql and looks_like_sql(sql):
                sql_in(d, sql, m.start(1) + (3 if m.group(1).startswith('"""') else 2 if m.group(1).startswith("@") else 1), "tsql")

        # gRPC
        for m in _GRPC.finditer(text):
            service = m.group(3)
            if m.group(2) != service:
                continue
            body_start = m.end() - 1
            body = text[body_start: matching(text, body_start)]
            for fm in re.finditer(r"\boverride\s+(?:async\s+)?[\w<>.,\s\[\]?]+?\s(\w+)\s*\(", body):
                a = body_start + fm.start(1)
                d.endpoint(d.at(a, a + len(fm.group(1))), "RPC", f"/{service}/{fm.group(1)}", "grpc-dotnet", f"{m.group(1)}.{fm.group(1)}", protocol="grpc")

        # Topics
        for rx, role, transport in _TOPICS:
            if transport == "kafka" and "Confluent.Kafka" not in raw:
                continue
            if transport == "servicebus" and "ServiceBus" not in raw:
                continue
            for m in rx.finditer(text):
                arg = m.group(1)
                lit = _lit(arg) if arg[:1] in '"@' else None
                topic = queue_name(lit) if lit is not None else queue_name(consts[arg.split(".")[-1]]) if arg.split(".")[-1] in consts else None
                d.topic(d.at(m.start(), m.end()), role, transport, topic, arg if lit is None and topic is None and re.fullmatch(r"[A-Za-z_][\w.]*", arg) else None)

        if any(m in raw for m in _RESILIENCE_MARKERS):
            code_traits(d, _dotnet_rules(raw, text), "c")
        if not d.out:
            return Extraction(parsed=False)
        return d.done() if d.resolved or d.unresolved else Extraction(d.out)


EXTRACTORS = [DotnetSource()]
