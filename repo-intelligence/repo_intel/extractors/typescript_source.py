"""TypeScript / JavaScript source: Express routes, Next.js App Router handlers,
process.env config keys, and linker observations (imports, exported symbols,
HTTP client calls, Prisma client call sites, unsafe raw SQL, test cases).

Parsing is done with tree-sitter only; repository code is never executed.
Configuration VALUES are never copied: a ConfigKey carries its name, and an
http_call carries only the static path text of the URL (no query string, no
userinfo, interpolations replaced by "{}") and, when the source states it, the
HTTP method in upper case.
"""

from __future__ import annotations

import posixpath
import re
from functools import cache

from repo_intel.compile.ontology import EntityKind, Predicate
from repo_intel.extractors.base import PACKAGE, UNIT, Extraction, diagnostic, entity, obs, ref, relation, span

EXTENSIONS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")
EXPRESS_METHODS = {"get", "post", "put", "patch", "delete"}
NEXT_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
NEXT_ROUTE_FILES = {"route.ts", "route.js", "route.tsx"}
_ROUTERISH = re.compile(r"(app|router|routes?|api|server)$", re.I)
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_USERINFO = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*://)[^/@]*@")
# Message-broker clients. kafkajs (and the Confluent client's kafkajs API) take
# `{ topic }` / `{ topics }` objects; node-rdkafka takes the topic positionally.
MESSAGING_PACKAGES = {"kafkajs": "kafka", "node-rdkafka": "kafka", "@confluentinc/kafka-javascript": "kafka"}
_TOPIC_NAME = re.compile(r"^[A-Za-z0-9._-]{1,249}$")  # the characters a Kafka topic may contain
_SECRET_LIKE = re.compile(r"KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL", re.I)
_WRAPPERS = ("as_expression", "satisfies_expression", "parenthesized_expression", "non_null_expression")
_DECLARATIONS = {
    "function_declaration": "function", "generator_function_declaration": "function", "function_signature": "function",
    "class_declaration": "class", "abstract_class_declaration": "class",
    "interface_declaration": "interface", "type_alias_declaration": "type", "enum_declaration": "enum",
    "lexical_declaration": "const", "variable_declaration": "const",
}


@cache
def _parser(grammar: str):
    from tree_sitter import Language, Parser

    if grammar == "javascript":
        import tree_sitter_javascript

        return Parser(Language(tree_sitter_javascript.language()))
    import tree_sitter_typescript

    return Parser(Language(tree_sitter_typescript.language_tsx() if grammar == "tsx" else tree_sitter_typescript.language_typescript()))


def _grammar(path: str) -> str:
    ext = posixpath.splitext(path)[1].lower()
    return "tsx" if ext in (".tsx", ".jsx") else "typescript" if ext == ".ts" else "javascript"


def next_route(path: str) -> str | None:
    """URL path of a Next.js App Router `route` file, or None if it is not one."""
    parts = path.split("/")
    if parts[-1] not in NEXT_ROUTE_FILES or "app" not in parts[:-1]:
        return None
    last_app = len(parts) - 2 - parts[-2::-1].index("app")
    segments = [s for s in parts[last_app + 1 : -1] if not (s.startswith("(") and s.endswith(")")) and not s.startswith("@")]
    return "/" + "/".join(segments)


def _text(node) -> str:
    return node.text.decode("utf-8", "replace")


def _string(node) -> str | None:
    """Value of a string literal, or of a template literal without interpolation."""
    if node is None:
        return None
    if node.type == "string":
        return _text(node)[1:-1]
    if node.type == "template_string" and not any(c.type == "template_substitution" for c in node.children):
        return _text(node)[1:-1]
    return None


def _template(data: bytes, node) -> str:
    out, at = [], node.start_byte + 1
    for c in node.children:
        if c.type == "template_substitution":
            out.append(data[at : c.start_byte].decode("utf-8", "replace") + "{}")
            at = c.end_byte
    out.append(data[at : node.end_byte - 1].decode("utf-8", "replace"))
    return "".join(out)


def _static_path(text: str) -> str:
    """Drop the parts of a URL that can carry secrets: userinfo, query, fragment."""
    text = _USERINFO.sub(r"\1", text)
    return re.split(r"[?#]", text, maxsplit=1)[0]


def _args(call) -> list:
    arguments = call.child_by_field_name("arguments")
    return [c for c in arguments.named_children if c.type != "comment"] if arguments is not None and arguments.type == "arguments" else []


def _tail_name(node) -> str | None:
    """`app` for `app`, `router` for `this.router`."""
    if node is None:
        return None
    if node.type == "identifier":
        return _text(node)
    if node.type == "member_expression":
        prop = node.child_by_field_name("property")
        return _text(prop) if prop is not None else None
    return None


def _is_process_env(node) -> bool:
    if node is None or node.type != "member_expression":
        return False
    o, p = node.child_by_field_name("object"), node.child_by_field_name("property")
    return o is not None and p is not None and o.type == "identifier" and o.text == b"process" and p.text == b"env"


def _unwrap(node):
    """`x` for `x as const`, `(x)`, `x satisfies T` and `x!`."""
    while node is not None and node.type in _WRAPPERS:
        inner = [c for c in node.named_children if c.type not in ("type_identifier", "generic_type", "predefined_type", "object_type", "union_type", "array_type")]
        node = inner[0] if inner else None
    return node


def _pairs(obj, key: str) -> list:
    """Values of `key` in an object literal; `{ key }` shorthand yields the identifier."""
    found = []
    for c in obj.named_children:
        if c.type == "pair":
            k = c.child_by_field_name("key")
            if k is not None and (_text(k) == key or _string(k) == key):
                found.append(_unwrap(c.child_by_field_name("value")))
        elif c.type == "shorthand_property_identifier" and _text(c) == key:
            found.append(c)
    return [f for f in found if f is not None]


def _topic_values(obj) -> list:
    """The topic expressions of a kafkajs `{ topic }` or `{ topics: [...] }` argument."""
    values = _pairs(obj, "topic")
    for topics in _pairs(obj, "topics"):
        values += list(topics.named_children) if topics.type == "array" else [topics]
    return values


def _dotted_name(node) -> bool:
    """True for `Topics.ORDERS` or `config.topics.orders`: identifiers joined by dots."""
    if node is None or node.type != "member_expression":
        return False
    while node.type == "member_expression":
        prop = node.child_by_field_name("property")
        if prop is None or prop.type != "property_identifier":
            return False
        node = node.child_by_field_name("object")
    return node.type == "identifier"


def _walk(root):
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(reversed(node.children))


PRISMA_READS = {"findMany", "findUnique", "findUniqueOrThrow", "findFirst", "findFirstOrThrow", "count", "aggregate", "groupBy"}
PRISMA_WRITES = {"create", "createMany", "createManyAndReturn", "update", "updateMany", "updateManyAndReturn", "upsert", "delete", "deleteMany"}
PRISMA_UNSAFE = {"$queryRawUnsafe", "$executeRawUnsafe"}
_PRISMA_MODULE = re.compile(r"(^|/)(prisma|db)(\.[a-z]+)?$|^@prisma/client$")
_TEST_FILE = re.compile(r"(\.(test|spec)\.[cm]?[jt]sx?$)|(^|/)__tests__/")
TEST_FRAMEWORKS = {"vitest": "vitest", "@jest/globals": "jest", "node:test": "node:test", "@playwright/test": "playwright", "mocha": "mocha"}
# Calls that ask a hosted model for a completion (OpenAI, Anthropic, Azure OpenAI, Gemini, Bedrock).
MODEL_CALL = re.compile(r"(^|\.)(messages\.create|chat\.completions\.create|completions\.create|responses\.create|generateContent|converse)$")
# A comparison against one of these names bounds a loop (turns < MAX_TURNS, steps >= limit).
LOOP_BOUND = re.compile(r"max|limit|turn|step|iter|budget|attempt|count|remaining", re.I)
_CONCURRENCY_LIMITERS = (b"p-limit", b"p-map", b"p-queue", b"bottleneck", b"async-sema")
_HTTP_CLIENTS = re.compile(r"^(axios(\.\w+)?|got(\.\w+)?|ky(\.\w+)?|\w+\.(get|post|put|patch|delete|request))$")
_FUNCTIONS = ("arrow_function", "function_expression", "function", "function_declaration", "generator_function_declaration", "method_definition")


def is_test_file(path: str) -> bool:
    return bool(_TEST_FILE.search(path))


# -- HTTP client calls (the `http_call` observation) -----------------------------

_HTTP_VERBS = ("get", "post", "put", "patch", "delete", "head", "options")
_HTTP_METHOD = re.compile(r"^[A-Za-z]{3,7}$")
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")


def _receiver_name(node) -> str | None:
    """`api` for `api` and for `this.api`; None for anything else."""
    if node is None:
        return None
    if node.type == "identifier":
        return _text(node)
    if node.type == "member_expression":
        obj, prop = node.child_by_field_name("object"), node.child_by_field_name("property")
        if obj is not None and obj.type == "this" and prop is not None:
            return "this." + _text(prop)
    return None


def _http_clients(nodes, data: bytes) -> dict[str, dict[str, str]]:
    """The HTTP clients a file names, by the name a call reaches them through.

    `axios`: local names of the axios module (`import axios from "axios"`, `import * as`,
    `require("axios")`). `axios_instance`: name -> base path of `const api = axios.create({
    baseURL })` (the static base when literal, `{}` when computed, empty when absent).
    `angular`: names of an injected Angular `HttpClient` (a constructor parameter or field
    typed `HttpClient`, or `inject(HttpClient)`), only when the file imports it from
    `@angular/common/http`; a field is reached as `this.<name>`. AngularJS `$http` needs no
    import: it is recognised by its name."""
    axios: set[str] = set()
    angular_imported = False
    for n in nodes:
        if n.type == "import_statement":
            source = _string(n.child_by_field_name("source"))
            if source == "axios":
                for clause in (c for c in n.children if c.type == "import_clause"):
                    for c in clause.children:
                        if c.type == "identifier":
                            axios.add(_text(c))
                        elif c.type == "namespace_import":
                            axios.update(_text(i) for i in c.named_children if i.type == "identifier")
            elif source == "@angular/common/http" and b"HttpClient" in n.text:
                angular_imported = True
        elif n.type == "variable_declarator":
            name, value = n.child_by_field_name("name"), _unwrap(n.child_by_field_name("value"))
            if name is None or name.type != "identifier" or value is None:
                continue
            if value.type == "member_expression" and _tail_name(value) == "default":
                value = value.child_by_field_name("object")
            fn = value.child_by_field_name("function") if value.type == "call_expression" else None
            if fn is not None and fn.type == "identifier" and fn.text == b"require":
                args = _args(value)
                if args and _string(args[0]) == "axios":
                    axios.add(_text(name))
    clients: dict[str, dict[str, str]] = {"axios": {name: "" for name in sorted(axios)}, "axios_instance": {}, "angular": {}}
    for n in nodes:
        holder = name = value = None
        if n.type == "variable_declarator":
            name, value = n.child_by_field_name("name"), _unwrap(n.child_by_field_name("value"))
            holder = _text(name) if name is not None and name.type == "identifier" else None
        elif n.type in ("public_field_definition", "field_definition"):
            name, value = n.child_by_field_name("name") or n.child_by_field_name("property"), _unwrap(n.child_by_field_name("value"))
            holder = "this." + _text(name) if name is not None else None
        elif n.type in ("required_parameter", "optional_parameter") and angular_imported:
            pattern, annotation = n.child_by_field_name("pattern"), n.child_by_field_name("type")
            if pattern is not None and pattern.type == "identifier" and annotation is not None and _text(annotation).lstrip(":").strip() == "HttpClient":
                # A constructor parameter with an access modifier is also a field.
                clients["angular"][_text(pattern)] = ""
                if any(c.type == "accessibility_modifier" or c.type == "readonly" for c in n.children):
                    clients["angular"]["this." + _text(pattern)] = ""
            continue
        if holder is None:
            continue
        annotation = n.child_by_field_name("type")
        if angular_imported and annotation is not None and _text(annotation).lstrip(":").strip() == "HttpClient":
            clients["angular"][holder] = ""
        if value is None or value.type != "call_expression":
            continue
        fn = value.child_by_field_name("function")
        args = _args(value)
        if angular_imported and fn is not None and _text(fn) == "inject" and args and _text(args[0]) == "HttpClient":
            clients["angular"][holder] = ""
        elif fn is not None and fn.type == "member_expression" and _tail_name(fn) == "create" and _receiver_name(fn.child_by_field_name("object")) in clients["axios"]:
            config = _unwrap(args[0]) if args else None
            base = ""
            if config is not None and config.type == "object":
                found = _pairs(config, "baseURL")
                if found:
                    text = _url_text(data, found[0])
                    base = _static_path(text) if text is not None else "{}"
                elif any(c.type == "spread_element" for c in config.named_children):
                    base = "{}"  # a spread config may carry a base URL
            elif config is not None:
                base = "{}"  # a config built elsewhere may carry a base URL
            clients["axios_instance"][holder] = base
    return clients


def _method_literal(node) -> str | None:
    value = _string(_unwrap(node)) if node is not None else None
    return value.upper() if value is not None and _HTTP_METHOD.match(value) else None


def _config_url(config) -> tuple[object, str | None]:
    """(URL node, method) of an axios or `$http` config object literal."""
    if config is None or config.type != "object":
        return None, None
    urls, methods = _pairs(config, "url"), _pairs(config, "method")
    return (urls[0] if urls else None), (_method_literal(methods[0]) if methods else None)


def _http_call(fn, args, clients) -> tuple[str, object, str | None, str] | None:
    """(client, URL node, method, base path) when `fn(args)` is an HTTP client call."""
    if fn.type == "identifier" and fn.text == b"fetch" or fn.type == "member_expression" and _tail_name(fn) == "fetch" and _text(fn.child_by_field_name("object")) in ("window", "globalThis", "self"):
        if not args:
            return None
        options = _unwrap(args[1]) if len(args) > 1 else None
        methods = _pairs(options, "method") if options is not None and options.type == "object" else []
        return "fetch", args[0], _method_literal(methods[0]) if methods else None, ""
    name = _receiver_name(fn)
    if name is not None:  # called directly: axios(config), axios(url, config), $http(config), api(config)
        base = clients["axios"].get(name, clients["axios_instance"].get(name))
        if base is None and name not in ("$http", "this.$http"):
            return None
        client = "$http" if base is None else "axios"
        first = _unwrap(args[0]) if args else None
        if first is not None and first.type == "object":
            url, method = _config_url(first)
            return client, url, method, base or ""
        if client == "axios" and first is not None:
            options = _unwrap(args[1]) if len(args) > 1 else None
            return client, first, _config_url(options)[1] if options is not None else None, base or ""
        return client, None, None, base or ""
    if fn.type != "member_expression":
        return None
    verb = _text(fn.child_by_field_name("property")) if fn.child_by_field_name("property") is not None else ""
    receiver = _receiver_name(fn.child_by_field_name("object"))
    if receiver is None:
        return None
    if receiver in clients["axios"] or receiver in clients["axios_instance"]:
        client, base = "axios", clients["axios"].get(receiver, clients["axios_instance"].get(receiver, ""))
    elif receiver in ("$http", "this.$http"):
        client, base = "$http", ""
    elif receiver in clients["angular"]:
        client, base = "HttpClient", ""
    else:
        return None
    if verb in _HTTP_VERBS or verb == "jsonp" and client != "axios":
        return client, args[0] if args else None, "GET" if verb == "jsonp" else verb.upper(), base
    if verb == "request" and client == "axios":
        url, method = _config_url(_unwrap(args[0]) if args else None)
        return client, url, method, base
    if verb == "request" and client == "HttpClient" and len(args) > 1:
        return client, args[1], _method_literal(args[0]), base
    return None


def _url_text(data: bytes, node) -> str | None:
    """The URL a call names when it is a string or template literal (interpolations as `{}`)."""
    node = _unwrap(node) if node is not None else None
    if node is None:
        return None
    if node.type == "string":
        return _text(node)[1:-1]
    if node.type == "template_string":
        return _template(data, node)
    return None


def _join_base(base: str, path: str) -> str:
    """An axios instance's base URL before a call's path; an absolute URL ignores the base."""
    if not base or _SCHEME.match(path):
        return path
    if not path:
        return base
    return base.rstrip("/") + "/" + path.lstrip("/")


class TypeScriptSource:
    id = "typescript.source"
    version = "1.6.0"  # 1.6.0: symbols carry `exported`, default exports of a local name carry `local`

    def matches(self, path: str) -> bool:
        return path.lower().endswith(EXTENSIONS)

    def extract(self, path: str, data: bytes, blob: str) -> Extraction:
        tree = _parser(_grammar(path)).parse(data)
        root = tree.root_node
        out = Extraction()
        if root.has_error:
            # tree-sitter recovers around errors; what it still found is kept.
            out.diagnostics.append(diagnostic(self.id, "warning", "syntax errors: extraction may be incomplete"))
            out.parsed = False
        nodes = list(_walk(root))
        at = lambda a, b: span(data, a.start_byte, b.end_byte)

        self._imports(nodes, blob, at, out)
        self._symbols(root, blob, at, out)
        self._config(nodes, blob, at, out)
        self._fetch(nodes, data, blob, at, out)
        self._string_constants(root, blob, at, out)
        transport = self._messaging(nodes)
        if transport:
            self._topic_uses(nodes, transport, blob, at, out)
        inline: dict[int, str] = {}  # inline route callback node id -> "METHOD /route"
        if self._uses_express(nodes):
            self._express(nodes, blob, at, out, inline)
        route = next_route(path)
        if route is not None:
            self._next(root, route, blob, at, out)
        self._prisma(nodes, blob, at, out, inline)
        self._traits(nodes, data, blob, at, out, inline)
        if is_test_file(path):
            self._tests(nodes, blob, at, out)
        return out

    # -- the function a node is in ---------------------------------------------

    @staticmethod
    def _enclosing(node, inline: dict[int, str]) -> tuple[str, str]:
        """(name, kind) of the function `node` is in: a declared function, `Class.method`, a
        `const f = () => ...`, an inline route callback named after its route, else the module."""
        current = node.parent
        while current is not None:
            if current.type in _FUNCTIONS:
                if current.id in inline:
                    return inline[current.id], "route_handler"
                name = current.child_by_field_name("name")
                if current.type == "method_definition" and name is not None:
                    cls = current.parent.parent if current.parent is not None else None
                    cls_name = cls.child_by_field_name("name") if cls is not None and cls.type in ("class_declaration", "abstract_class_declaration", "class") else None
                    return (f"{_text(cls_name)}.{_text(name)}" if cls_name is not None else _text(name)), "method"
                if name is not None and current.type != "arrow_function":
                    return _text(name), "function"
                holder = current.parent
                if holder is not None and holder.type == "variable_declarator" and holder.child_by_field_name("name") is not None and holder.child_by_field_name("name").type == "identifier":
                    return _text(holder.child_by_field_name("name")), "function"
            current = current.parent
        return "<module>", "module"

    # -- code traits ----------------------------------------------------------------

    def _traits(self, nodes, data, blob, at, out, inline):
        """Code traits the antipattern detectors read (spec §8.8): axios-retry policies and
        whether they back off, `Promise.all` over a mapped collection with no concurrency
        limiter, and model-calling loops that dispatch tools with no iteration bound."""
        trait = lambda node, payload: out.observations.append(obs("code_trait", blob, at(node, node), {"function": self._enclosing(node, inline)[0], **payload}, ast_node_kind=node.type))
        limited = any(lib in data for lib in _CONCURRENCY_LIMITERS)
        for call in (n for n in nodes if n.type == "call_expression"):
            callee = _text(call.child_by_field_name("function")) if call.child_by_field_name("function") is not None else ""
            args = _args(call)
            if callee in ("axiosRetry", "axiosRetry.default") and b"axios-retry" in data:
                options = args[1] if len(args) > 1 and args[1].type == "object" else None
                keys = {_text(c.child_by_field_name("key")) for c in options.named_children if c.type == "pair"} if options is not None else set()
                trait(call, {"trait": "retry", "library": "axios-retry", "backoff": "retryDelay" in keys, "bounded": True})
            elif callee in ("Promise.all", "Promise.allSettled") and not limited and args:
                inner = _unwrap(args[0])
                fn = inner.child_by_field_name("function") if inner is not None and inner.type == "call_expression" else None
                if fn is not None and fn.type == "member_expression" and _tail_name(fn) == "map":
                    trait(call, {"trait": "unbounded_fanout", "construct": callee + " over .map"})
        for loop in (n for n in nodes if n.type in ("while_statement", "for_statement")):
            condition = loop.child_by_field_name("condition")
            inside = list(_walk(loop))
            if not any(n.type == "call_expression" and n.child_by_field_name("function") is not None and MODEL_CALL.search(_text(n.child_by_field_name("function"))) for n in inside):
                continue
            if b"tool" not in data[loop.start_byte:loop.end_byte]:
                continue
            cond = _text(_unwrap(condition)) if condition is not None and _unwrap(condition) is not None else ""
            if loop.type == "for_statement" and cond.strip(" ;") not in ("", "true"):
                continue  # a counted for loop
            open_ended = cond.strip(" ;") in ("", "true", "1") or (_unwrap(condition) is not None and _unwrap(condition).type == "identifier") or re.fullmatch(r"!\s*[A-Za-z_$][\w$]*", cond.strip(" ;")) is not None
            bounded = any(n.type == "binary_expression" and re.search(r"[<>]=?", _text(n.child_by_field_name("operator")) if n.child_by_field_name("operator") is not None else "") and LOOP_BOUND.search(_text(n)) for n in inside)
            trait(condition or loop, {"trait": "agent_loop", "bounded": not open_ended or bounded})
        self._resilience(nodes, data, trait)

    @staticmethod
    def _resilience(nodes, data, trait):
        """Resilience policies (spec §8.8): circuit breakers (opossum, cockatiel), bulkheads
        (cockatiel) and concurrency limits (p-limit, p-queue), timeouts (`AbortSignal.timeout`,
        cockatiel, a `timeout` option on an HTTP client or call) and rate limiters
        (express-rate-limit, rate-limiter-flexible, @nestjs/throttler inbound; Bottleneck
        outbound). Each is a library's own construct, never a dependency alone."""
        has = lambda *libs: any(lib.encode() in data for lib in libs)
        for node in nodes:
            if node.type not in ("call_expression", "new_expression"):
                continue
            target = node.child_by_field_name("function" if node.type == "call_expression" else "constructor")
            callee = _text(target) if target is not None else ""
            last = callee.rsplit(".", 1)[-1]
            args = _args(node)
            if node.type == "new_expression" and last == "CircuitBreaker" and has("opossum"):
                trait(node, {"trait": "circuit_breaker", "library": "opossum"})
            elif last == "circuitBreaker" and has("cockatiel"):
                trait(node, {"trait": "circuit_breaker", "library": "cockatiel"})
            elif last == "bulkhead" and has("cockatiel"):
                trait(node, {"trait": "bulkhead", "library": "cockatiel"})
            elif last in ("pLimit", "limit") and has("p-limit") and node.type == "call_expression" and args and args[0].type == "number":
                trait(node, {"trait": "concurrency_limit", "name": TypeScriptSource._declared(node) or callee, "construct": "p-limit"})
            elif node.type == "new_expression" and last == "PQueue" and has("p-queue"):
                trait(node, {"trait": "concurrency_limit", "name": TypeScriptSource._declared(node) or "PQueue", "construct": "p-queue"})
            elif callee == "AbortSignal.timeout":
                trait(node, {"trait": "timeout", "construct": callee, "deadline": False})
            elif last == "timeout" and has("cockatiel") and node.type == "call_expression":
                trait(node, {"trait": "timeout", "construct": "cockatiel timeout", "deadline": False})
            elif last in ("rateLimit", "slowDown") and has("express-rate-limit", "express-slow-down"):
                trait(node, {"trait": "rate_limit", "library": "express-rate-limit", "direction": "inbound"})
            elif node.type == "new_expression" and last.startswith("RateLimiter") and has("rate-limiter-flexible"):
                trait(node, {"trait": "rate_limit", "library": "rate-limiter-flexible", "direction": "inbound"})
            elif last == "forRoot" and callee.startswith("ThrottlerModule") and has("@nestjs/throttler"):
                trait(node, {"trait": "rate_limit", "library": "@nestjs/throttler", "direction": "inbound"})
            elif node.type == "new_expression" and last == "Bottleneck" and has("bottleneck"):
                trait(node, {"trait": "rate_limit", "library": "bottleneck", "direction": "outbound"})
            elif (_HTTP_CLIENTS.match(callee) or callee in ("axios.create", "got.extend", "ky.create")) and has("axios", "got", "ky", "undici"):
                for arg in args:
                    if arg.type == "object" and any(c.type == "pair" and _text(c.child_by_field_name("key")) == "timeout" for c in arg.named_children):
                        trait(node, {"trait": "timeout", "construct": callee, "deadline": False})
                        break

    @staticmethod
    def _declared(node) -> str | None:
        """The name a value is declared or assigned to (`const limit = pLimit(4)`)."""
        parent = node.parent
        if parent is not None and parent.type == "variable_declarator" and parent.child_by_field_name("name") is not None:
            return _text(parent.child_by_field_name("name"))
        if parent is not None and parent.type == "assignment_expression" and parent.child_by_field_name("left") is not None:
            return _text(parent.child_by_field_name("left"))
        return None

    # -- Prisma client ------------------------------------------------------------

    def _prisma(self, nodes, blob, at, out, inline):
        """`prisma.order.findMany(...)` becomes an orm_use of model delegate `order`;
        `$queryRawUnsafe` / `$executeRawUnsafe` are dynamic SQL. A client is a
        `new PrismaClient()`, a name imported from a prisma/db module, `this.prisma`,
        or the callback parameter of `client.$transaction(...)`."""
        clients: set[str] = set()
        imports_prisma = False
        for n in nodes:
            if n.type == "import_statement":
                source = _string(n.child_by_field_name("source")) or ""
                if source == "@prisma/client":
                    imports_prisma = True
                if _PRISMA_MODULE.search(source) and source != "@prisma/client":
                    for c in _walk(n):
                        if c.type == "identifier" and c.parent is not None and c.parent.type in ("import_clause", "import_specifier", "namespace_import"):
                            if c.parent.type != "import_specifier" or c == (c.parent.child_by_field_name("alias") or c.parent.child_by_field_name("name")):
                                clients.add(_text(c))
            elif n.type == "variable_declarator":
                value, name = n.child_by_field_name("value"), n.child_by_field_name("name")
                if value is not None and name is not None and name.type == "identifier" and _text(value).startswith("new PrismaClient"):
                    clients.add(_text(name))
        if not clients and not imports_prisma:
            return

        def is_client(node) -> bool:
            if node is None:
                return False
            if node.type == "identifier":
                return _text(node) in clients
            return node.type == "member_expression" and _tail_name(node) in ("prisma", *clients)

        for n in nodes:  # transaction callbacks: `prisma.$transaction(async (tx) => tx.order.create(...))`
            fn = n.child_by_field_name("function") if n.type == "call_expression" else None
            if fn is not None and fn.type == "member_expression" and _tail_name(fn) == "$transaction" and is_client(fn.child_by_field_name("object")):
                for arg in _args(n):
                    params = arg.child_by_field_name("parameters") if arg.type in ("arrow_function", "function_expression") else None
                    first = params.named_children[0] if params is not None and params.named_children else (arg.child_by_field_name("parameter") if arg.type == "arrow_function" else None)
                    ident = first.child_by_field_name("pattern") if first is not None and first.type in ("required_parameter", "optional_parameter") else first
                    if ident is not None and ident.type == "identifier":
                        clients.add(_text(ident))
        seen: set[tuple[str, str, str]] = set()
        for n in nodes:
            fn = n.child_by_field_name("function") if n.type == "call_expression" else None
            if fn is None or fn.type != "member_expression":
                continue
            op = _tail_name(fn)
            receiver = fn.child_by_field_name("object")
            if op in PRISMA_UNSAFE and is_client(receiver):
                function, kind = self._enclosing(n, inline)
                out.observations.append(obs("dynamic_sql", blob, at(n, n), {"function": function, "function_kind": kind}, ast_node_kind=n.type))
                continue
            if op not in PRISMA_READS and op not in PRISMA_WRITES or receiver is None or receiver.type != "member_expression":
                continue
            delegate = _tail_name(receiver)
            if not delegate or delegate.startswith("$") or not is_client(receiver.child_by_field_name("object")):
                continue
            function, kind = self._enclosing(n, inline)
            access = "read" if op in PRISMA_READS else "write"
            if (function, delegate, access) in seen:
                continue
            seen.add((function, delegate, access))
            out.observations.append(obs("orm_use", blob, at(fn, fn), {"function": function, "function_kind": kind, "model": delegate, "access": access, "orm": "prisma"}, ast_node_kind=n.type))

    # -- tests ----------------------------------------------------------------------

    def _tests(self, nodes, blob, at, out):
        """`it("...")` / `test("...")` cases, named with their `describe` blocks: "Cart > adds"."""
        framework = "jest"
        for n in nodes:
            if n.type == "import_statement" and _string(n.child_by_field_name("source")) in TEST_FRAMEWORKS:
                framework = TEST_FRAMEWORKS[_string(n.child_by_field_name("source"))]
                break

        def title(call) -> str | None:
            fn = call.child_by_field_name("function")
            head = _text(fn).split(".")[0] if fn is not None and fn.type in ("identifier", "member_expression") else None
            args = _args(call)
            if head not in ("it", "test", "describe") or not args or (fn.type == "member_expression" and _tail_name(fn) not in ("only", "skip", "concurrent")):
                return None
            return _string(args[0])

        for n in nodes:
            if n.type != "call_expression":
                continue
            fn = n.child_by_field_name("function")
            name = title(n)
            if name is None or _text(fn).split(".")[0] == "describe":
                continue
            chain, current = [name], n.parent
            while current is not None:
                if current.type == "call_expression" and _text(current.child_by_field_name("function")).split(".")[0] == "describe" and title(current):
                    chain.insert(0, title(current))
                current = current.parent
            out.observations.append(obs("test", blob, at(n, n), {"name": " > ".join(chain), "framework": framework}, ast_node_kind=n.type))

    # -- imports -----------------------------------------------------------

    def _imports(self, nodes, blob, at, out):
        for n in nodes:
            if n.type == "import_statement":
                source = _string(n.child_by_field_name("source"))
                if source is None:
                    continue
                names = []
                for clause in (c for c in n.children if c.type == "import_clause"):
                    for c in clause.children:
                        if c.type == "identifier":
                            names.append("default")
                        elif c.type == "namespace_import":
                            names.append("*")
                        elif c.type == "named_imports":
                            names += [_text(s.child_by_field_name("name")) for s in c.named_children if s.type == "import_specifier" and s.child_by_field_name("name") is not None]
                out.observations.append(obs("import", blob, at(n, n), {"specifier": source, "names": names, "form": "import"}, ast_node_kind=n.type))
            elif n.type == "export_statement" and n.child_by_field_name("source") is not None:
                source = _string(n.child_by_field_name("source"))
                if source is None:
                    continue
                names = []
                for c in n.children:
                    if c.type == "*" or c.type == "namespace_export":
                        names.append("*")
                    elif c.type == "export_clause":
                        names += [_text(s.child_by_field_name("name")) for s in c.named_children if s.type == "export_specifier" and s.child_by_field_name("name") is not None]
                out.observations.append(obs("import", blob, at(n, n), {"specifier": source, "names": names, "form": "reexport"}, ast_node_kind=n.type))
            elif n.type == "call_expression":
                fn = n.child_by_field_name("function")
                if fn is None or not ((fn.type == "identifier" and fn.text == b"require") or fn.type == "import"):
                    continue
                args = _args(n)
                source = _string(args[0]) if args else None
                if source is not None:
                    out.observations.append(obs("import", blob, at(n, n), {"specifier": source, "names": [], "form": "require" if fn.type == "identifier" else "dynamic"}, ast_node_kind=n.type))

    def _uses_express(self, nodes) -> bool:
        for n in nodes:
            if n.type == "import_statement" and _string(n.child_by_field_name("source")) == "express":
                return True
            if n.type == "import_require_clause" and any(_string(c) == "express" for c in n.children):
                return True
            if n.type == "call_expression":
                fn = n.child_by_field_name("function")
                if fn is not None and fn.type == "identifier" and fn.text == b"require":
                    args = _args(n)
                    if args and _string(args[0]) == "express":
                        return True
        return False

    # -- exported symbols --------------------------------------------------

    def _exports(self, root):
        """(name, symbol_kind, export statement, name node) for each top-level export."""
        for st in root.children:
            if st.type != "export_statement" or st.child_by_field_name("source") is not None:
                continue
            is_default = any(c.type == "default" for c in st.children)
            decl = st.child_by_field_name("declaration")
            if decl is not None and decl.type in _DECLARATIONS:
                if decl.type in ("lexical_declaration", "variable_declaration"):
                    for d in decl.named_children:
                        name = d.child_by_field_name("name") if d.type == "variable_declarator" else None
                        if name is not None and name.type == "identifier":
                            yield _text(name), "const", st, name
                else:
                    name = decl.child_by_field_name("name")
                    if name is not None:
                        yield _text(name), _DECLARATIONS[decl.type], st, name
                    elif is_default:
                        yield "default", _DECLARATIONS[decl.type], st, decl
                continue
            if is_default:
                value = st.child_by_field_name("value")
                if value is not None:
                    kind = {"arrow_function": "function", "function_expression": "function", "function": "function", "class": "class"}.get(value.type, "const")
                    yield "default", kind, st, value
                continue
            for clause in (c for c in st.children if c.type == "export_clause"):
                for s in clause.named_children:
                    if s.type == "export_specifier":
                        name = s.child_by_field_name("alias") or s.child_by_field_name("name")
                        if name is not None:
                            yield _text(name), "const", s, name

    def _symbols(self, root, blob, at, out):
        """One `symbol` observation per top-level export, with `exported: true` (every symbol
        this extractor reports is an export). A default export of a name this file declares at
        top level (`const X = ...; export default X`, `export { X as default }`) also carries
        `local: X`, so the linker can find the default-exported Symbol by its local name."""
        declared = None
        for name, kind, st, name_node in self._exports(root):
            end = name_node if name_node.type in ("identifier", "type_identifier") else st
            payload = {"name": name, "symbol_kind": kind, "exported": True}
            local = self._default_local(st) if name == "default" else None
            if local is not None:
                declared = self._top_declared(root) if declared is None else declared
                if local in declared:
                    payload["local"] = local
            out.observations.append(obs("symbol", blob, at(st, end), payload, ast_node_kind=st.type))

    @staticmethod
    def _default_local(st) -> str | None:
        """The local name a default export names: `X` in `export default X` and in
        `export { X as default }`. None for any other default export."""
        if st.type == "export_statement":
            value = st.child_by_field_name("value")
            return _text(value) if value is not None and value.type == "identifier" else None
        if st.type == "export_specifier" and st.child_by_field_name("alias") is not None:
            name = st.child_by_field_name("name")
            return _text(name) if name is not None and name.type == "identifier" else None
        return None

    @staticmethod
    def _top_declared(root) -> set[str]:
        """Names declared at the top level of the file (functions, classes, variables and
        type declarations, exported or not). Imported names are not declarations."""
        names: set[str] = set()
        for st in root.children:
            decl = st.child_by_field_name("declaration") if st.type == "export_statement" else st
            if decl is None or decl.type not in _DECLARATIONS:
                continue
            if decl.type in ("lexical_declaration", "variable_declaration"):
                for d in decl.named_children:
                    name = d.child_by_field_name("name") if d.type == "variable_declarator" else None
                    if name is not None and name.type == "identifier":
                        names.add(_text(name))
            else:
                name = decl.child_by_field_name("name")
                if name is not None:
                    names.add(_text(name))
        return names

    # -- configuration keys (names only) -----------------------------------

    def _config(self, nodes, blob, at, out):
        seen: set[str] = set()

        def add(name: str, node):
            if name in seen or not _ENV_NAME.match(name):
                return
            seen.add(name)
            key = ref(EntityKind.CONFIG_KEY, {"name": name}, name)
            out.observations.append(entity(blob, at(node, node), EntityKind.CONFIG_KEY, {"name": name}, name))
            out.observations.append(relation(blob, at(node, node), PACKAGE, Predicate.REFERENCES_CONFIG, key))

        for n in nodes:
            if n.type == "member_expression" and _is_process_env(n.child_by_field_name("object")):
                prop = n.child_by_field_name("property")
                if prop is not None and prop.type == "property_identifier":
                    add(_text(prop), n)
            elif n.type == "subscript_expression" and _is_process_env(n.child_by_field_name("object")):
                name = _string(n.child_by_field_name("index"))
                if name is not None:
                    add(name, n)
            elif n.type == "variable_declarator" and _is_process_env(n.child_by_field_name("value")):
                pattern = n.child_by_field_name("name")
                if pattern is not None and pattern.type == "object_pattern":
                    for p in pattern.named_children:
                        if p.type == "shorthand_property_identifier_pattern":
                            add(_text(p), p)
                        elif p.type in ("pair_pattern", "object_assignment_pattern"):
                            k = p.child_by_field_name("key") or p.child_by_field_name("left")
                            if k is not None and k.type in ("property_identifier", "shorthand_property_identifier_pattern"):
                                add(_text(k), k)

    # -- outbound HTTP calls ------------------------------------------------

    def _fetch(self, nodes, data, blob, at, out):
        """One `http_call` per client call whose URL is in the source: `fetch`, axios (the
        module, `axios.create()` instances, `axios(config)`), AngularJS `$http` and Angular
        `HttpClient`. `method` is recorded, upper-case, only when the source states it: the
        verb a call is named by (`axios.post`, `http.get`) or a literal `method:` option.
        Nothing defaults: a `fetch` without a `method:` literal carries no method. A call
        whose URL is not a literal is not recorded and gets an info diagnostic."""
        clients = _http_clients(nodes, data)
        for n in nodes:
            if n.type != "call_expression":
                continue
            fn = n.child_by_field_name("function")
            if fn is None:
                continue
            args = _args(n)
            found = _http_call(fn, args, clients)
            if found is None:
                continue
            client, url, method, prefix = found
            text = _url_text(data, url)
            if text is None:
                out.diagnostics.append(diagnostic(self.id, "info", f"line {n.start_point[0] + 1}: {client} call URL is not a literal; no http_call recorded"))
                continue
            payload = {"path": _join_base(prefix, _static_path(text))}
            if method is not None:
                payload["method"] = method
            out.observations.append(obs("http_call", blob, at(fn, url), payload, ast_node_kind=n.type))

    # -- event topics ----------------------------------------------------------

    def _messaging(self, nodes) -> str | None:
        """Transport of the broker client this file imports, if any."""
        for n in nodes:
            source = None
            if n.type == "import_statement":
                source = _string(n.child_by_field_name("source"))
            elif n.type == "call_expression":
                fn = n.child_by_field_name("function")
                if fn is not None and ((fn.type == "identifier" and fn.text == b"require") or fn.type == "import"):
                    args = _args(n)
                    source = _string(args[0]) if args else None
            if source in MESSAGING_PACKAGES:
                return MESSAGING_PACKAGES[source]
        return None

    def _string_constants(self, root, blob, at, out):
        """Top-level `const NAME = "value"` and `const Topics = { KEY: "value" }`.

        Kept for the linker, which resolves a topic passed by name; an object
        member is recorded as `Topics.KEY`. Names that read like credentials are
        skipped so a value that may be a secret never enters a payload.
        """
        def emit(name, value_node, where):
            value = _string(value_node)
            if value is not None and _TOPIC_NAME.match(value) and not _SECRET_LIKE.search(name):
                out.observations.append(obs("string_constant", blob, at(where, where), {"name": name, "value": value}, ast_node_kind=where.type))

        for st in root.children:
            decl = st.child_by_field_name("declaration") if st.type == "export_statement" else st
            if decl is None or decl.type != "lexical_declaration" or not any(c.type == "const" for c in decl.children):
                continue
            for d in decl.named_children:
                name, value = (d.child_by_field_name("name"), _unwrap(d.child_by_field_name("value"))) if d.type == "variable_declarator" else (None, None)
                if name is None or name.type != "identifier" or value is None:
                    continue
                if value.type == "object":
                    for pair in value.named_children:
                        key = pair.child_by_field_name("key") if pair.type == "pair" else None
                        if key is not None and key.type in ("property_identifier", "string"):
                            emit(f"{_text(name)}.{_string(key) if key.type == 'string' else _text(key)}", _unwrap(pair.child_by_field_name("value")), pair)
                else:
                    emit(_text(name), value, d)

    def _topic_uses(self, nodes, transport, blob, at, out):
        """kafkajs `producer.send({ topic })`, `consumer.subscribe({ topic | topics })`,
        and node-rdkafka `producer.produce(topic, ...)`, `consumer.subscribe([topic])`.

        A topic is recorded as a literal, as a name for the linker to resolve,
        or as dynamic when it is computed at run time.
        """
        for n in nodes:
            if n.type != "call_expression":
                continue
            fn = n.child_by_field_name("function")
            method = _tail_name(fn) if fn is not None and fn.type == "member_expression" else None
            args = _args(n)
            if method not in ("send", "sendBatch", "produce", "subscribe") or not args:
                continue
            role = "subscribe" if method == "subscribe" else "publish"
            first = _unwrap(args[0])
            if first.type == "object":
                targets = _topic_values(first)
                if not targets and method == "sendBatch":
                    for pair in _pairs(first, "topicMessages"):
                        for item in (pair.named_children if pair.type == "array" else []):
                            targets += _topic_values(_unwrap(item)) if _unwrap(item).type == "object" else []
                if not targets:
                    continue  # `mailer.send({ to })`: an object without a topic is not a broker call
            elif method in ("produce", "subscribe"):
                targets = list(first.named_children) if first.type == "array" else [first]
            else:
                continue  # kafkajs `send` always takes an object
            for item in targets:
                item = _unwrap(item)
                payload: dict = {"role": role, "transport": transport}
                literal = _string(item)
                if literal is not None and _TOPIC_NAME.match(literal):
                    payload["topic"] = literal
                elif item.type in ("identifier", "shorthand_property_identifier") or _dotted_name(item):
                    payload["name"] = _text(item)
                else:
                    payload["dynamic"] = True
                out.observations.append(obs("topic_use", blob, at(n, n), payload, ast_node_kind=n.type))

    # -- Express ------------------------------------------------------------

    def _express(self, nodes, blob, at, out, inline=None):
        inline = {} if inline is None else inline
        routers: set[str] = set()
        for n in nodes:
            if n.type == "variable_declarator":
                value, name = n.child_by_field_name("value"), n.child_by_field_name("name")
                fn = value.child_by_field_name("function") if value is not None and value.type == "call_expression" else None
                if fn is not None and name is not None and name.type == "identifier" and _text(fn) in ("express", "express.Router", "Router"):
                    routers.add(_text(name))
        for n in nodes:
            if n.type != "call_expression":
                continue
            fn = n.child_by_field_name("function")
            prop = fn.child_by_field_name("property") if fn is not None and fn.type == "member_expression" else None
            if prop is None or _text(prop) not in EXPRESS_METHODS:
                continue
            method, args, receiver = _text(prop).upper(), _args(n), fn.child_by_field_name("object")
            handler = _text(args[-1]) if args and args[-1].type == "identifier" else None

            # router.route("/x").get(h).post(h)
            base = receiver
            while base is not None and base.type == "call_expression":
                inner = base.child_by_field_name("function")
                inner_prop = inner.child_by_field_name("property") if inner is not None and inner.type == "member_expression" else None
                if inner_prop is None:
                    base = None
                elif _text(inner_prop) == "route":
                    break
                elif _text(inner_prop) in EXPRESS_METHODS | {"all"}:
                    base = inner.child_by_field_name("object")
                else:
                    base = None
            if base is not None and base.type == "call_expression":
                route_args = _args(base)
                route = _string(route_args[0]) if route_args else None
                if route is None:
                    out.diagnostics.append(diagnostic(self.id, "info", f"line {n.start_point[0] + 1}: express {method} route path is not a string literal; no Endpoint emitted"))
                elif args:
                    self._endpoint(blob, at(base, base), method, route, "express", handler, out)
                    if args[-1].type in ("arrow_function", "function_expression", "function"):
                        inline[args[-1].id] = f"{method} {route}"
                continue

            name = _tail_name(receiver)
            known = name in routers
            if len(args) < 2:
                continue  # app.get("setting") reads a setting; a route needs a handler
            route = _string(args[0])
            if route is None:
                if known or (name and _ROUTERISH.search(name)):
                    out.diagnostics.append(diagnostic(self.id, "info", f"line {n.start_point[0] + 1}: express {method} route path is not a string literal; no Endpoint emitted"))
                continue
            if not known and not route.startswith(("/", "*")):
                continue  # e.g. cache.get("key", fallback) on something that is not a router
            self._endpoint(blob, at(fn, args[0]), method, route, "express", handler, out)
            if args[-1].type in ("arrow_function", "function_expression", "function"):
                inline[args[-1].id] = f"{method} {route}"

    # -- Next.js App Router ---------------------------------------------------

    def _next(self, root, route, blob, at, out):
        for name, kind, st, name_node in self._exports(root):
            if name in NEXT_METHODS and kind in ("function", "const"):
                self._endpoint(blob, at(st, name_node), name, route, "next", name, out)

    def _endpoint(self, blob, where, method, route, framework, handler, out):
        key = {"component_root": UNIT, "protocol": "http", "method": method, "route": route}
        out.observations.append(entity(blob, where, EntityKind.ENDPOINT, key, f"{method} {route}", {"framework": framework}))
        out.observations.append(obs("route_handler", blob, where, {"framework": framework, "method": method, "route": route, "handler": handler}))


EXTRACTORS = [TypeScriptSource()]
