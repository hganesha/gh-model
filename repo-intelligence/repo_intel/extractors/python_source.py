"""Python source: FastAPI routes, SQLAlchemy models, Alembic migrations, config keys.

Parsed with tree-sitter only; repository code is never imported or executed.
Everything here is local to one file. Cross-file facts (which function calls
which, which router is mounted where) are left to the linker, which reads the
"symbol", "import" and "route_handler" observations emitted below.

Configuration VALUES are never copied: a config key contributes its name only.
"""

from __future__ import annotations

import re

import posixpath

from collections.abc import Iterator
from typing import Any

import tree_sitter_python
from tree_sitter import Language, Node, Parser

from ..ontology import EntityKind, Predicate
from .base import PACKAGE, UNIT, Extraction, diagnostic, entity, obs, ref, relation, span

_LANGUAGE = Language(tree_sitter_python.language())

HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}
MULTI_METHOD_DECORATORS = {"api_route"}
FASTAPI_FACTORIES = {"FastAPI", "APIRouter"}
COLUMN_CALLS = {"mapped_column", "Column"}
NON_COLUMN_CALLS = {"relationship", "backref", "association_proxy", "synonym", "composite", "column_property", "query_expression", "deferred"}
TABLE_OPS = {"create_table", "drop_table"}
# Message-broker clients whose publish and subscribe calls name a topic (Tier B: Kafka only).
MESSAGING_MODULES = {"confluent_kafka": "kafka", "kafka": "kafka", "aiokafka": "kafka"}
PUBLISH_CALLS = {"produce", "send", "send_and_wait"}
SUBSCRIBE_CALLS = {"subscribe"}
_TOPIC_NAME = re.compile(r"^[A-Za-z0-9._-]{1,249}$")  # the characters a Kafka topic may contain
_SECRET_LIKE = re.compile(r"KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL", re.I)


def _text(node: Node | None) -> str:
    return node.text.decode("utf-8", "replace") if node is not None and node.text is not None else ""


def _walk(node: Node) -> Iterator[Node]:
    """Pre-order walk that never descends into a broken subtree."""
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type == "ERROR" or current.is_missing:
            continue
        yield current
        stack.extend(reversed(current.children))


def _literal(node: Node | None) -> str | None:
    """The value of a plain string literal; None for f-strings, bytes, escapes and non-strings."""
    if node is None:
        return None
    if node.type == "parenthesized_expression" and node.named_child_count == 1:
        return _literal(node.named_children[0])
    if node.type != "string":
        return None
    start = next((c for c in node.children if c.type == "string_start"), None)
    end = next((c for c in node.children if c.type == "string_end"), None)
    if start is None or end is None:
        return None
    prefix = _text(start).rstrip("\"'").lower()
    if "f" in prefix or "b" in prefix or "t" in prefix:
        return None
    if any(c.type == "interpolation" for c in node.children):
        return None
    body = node.text[start.end_byte - node.start_byte : end.start_byte - node.start_byte].decode("utf-8", "replace")
    if "\\" in body and "r" not in prefix:
        return None  # escape sequences: not worth guessing the decoded value
    return body


def _args(call: Node) -> tuple[list[Node], dict[str, Node]]:
    """Positional and keyword argument nodes of a call."""
    positional: list[Node] = []
    keywords: dict[str, Node] = {}
    arguments = call.child_by_field_name("arguments")
    for arg in arguments.named_children if arguments is not None else []:
        if arg.type == "keyword_argument":
            value = arg.child_by_field_name("value")
            if value is not None:
                keywords[_text(arg.child_by_field_name("name"))] = value
        elif arg.type not in ("comment", "list_splat", "dictionary_splat"):
            positional.append(arg)
    return positional, keywords


def _callee(node: Node | None) -> str:
    """Dotted text of a call's function ("op.create_table"), or "" if `node` is not a call."""
    return _text(node.child_by_field_name("function")) if node is not None and node.type == "call" else ""


def _last(dotted: str) -> str:
    return dotted.rsplit(".", 1)[-1]


def _assignment(statement: Node) -> Node | None:
    if statement.type == "expression_statement" and statement.named_child_count == 1 and statement.named_children[0].type == "assignment":
        return statement.named_children[0]
    return None


def _definition(statement: Node) -> Node:
    if statement.type == "decorated_definition":
        return statement.child_by_field_name("definition") or statement
    return statement


def _functions(statements: list[Node]) -> Iterator[tuple[str, Node]]:
    """Top-level functions as (name, def) and methods of top-level classes as (Class.method, def)."""
    for statement in statements:
        definition = _definition(statement)
        name = _text(definition.child_by_field_name("name"))
        if definition.type == "function_definition" and name:
            yield name, definition
        elif definition.type == "class_definition" and name:
            body = definition.child_by_field_name("body")
            for member in body.named_children if body is not None else []:
                method = _definition(member)
                if method.type == "function_definition" and method.child_by_field_name("name") is not None:
                    yield f"{name}.{_text(method.child_by_field_name('name'))}", method


def _class_assignments(cls: Node) -> list[tuple[Node, Node]]:
    """(statement, assignment) for each assignment directly in a class body."""
    body = cls.child_by_field_name("body")
    return [(s, a) for s in (body.named_children if body is not None else []) if (a := _assignment(s)) is not None]


def _is_sqlmodel_table(cls: Node) -> bool:
    """`class User(UserBase, table=True):`"""
    bases = cls.child_by_field_name("superclasses")
    return any(
        b.type == "keyword_argument" and _text(b.child_by_field_name("name")) == "table" and _text(b.child_by_field_name("value")) == "True"
        for b in (bases.named_children if bases is not None else [])
    )


def _annotated_model(annotation: Node | None) -> str | None:
    """`User`, `User | None`, `Optional[models.User]` -> the single class-like name, else None."""
    if annotation is None:
        return None
    text = "".join(_text(annotation).split())
    if text.startswith("Optional[") and text.endswith("]"):
        text = text[len("Optional[") : -1]
    options = [o for o in text.split("|") if o != "None"]
    if len(options) != 1:
        return None
    parts = options[0].split(".")
    return options[0] if all(p.isidentifier() for p in parts) and _classlike(parts[-1]) and not any(_classlike(p) for p in parts[:-1]) else None


def _built_model(call: Node | None) -> str | None:
    """The model an expression constructs or loads: Model(...), Model.model_validate(...), x.get(Model, ...)."""
    if call is None or call.type != "call":
        return None
    target = call.child_by_field_name("function")
    if _model_name(target) is not None:
        return _model_name(target)
    if target is not None and target.type == "attribute":
        verb = _text(target.child_by_field_name("attribute"))
        if verb in MODEL_FACTORIES:
            return _model_name(target.child_by_field_name("object"))
        positional = _args(call)[0]
        if verb == "get" and len(positional) >= 2:
            return _model_name(positional[0])
    return None


def _classlike(segment: str) -> bool:
    """Heuristic for a class name: Capitalised and not an ALL_CAPS constant."""
    return segment[:1].isupper() and segment.isidentifier() and not (len(segment) > 1 and segment.isupper())


def _dotted(node: Node | None) -> list[str] | None:
    """["models", "Order", "id"] for a pure name/attribute chain, else None."""
    if node is None:
        return None
    if node.type == "identifier":
        return [_text(node)]
    if node.type == "attribute":
        head = _dotted(node.child_by_field_name("object"))
        return None if head is None else [*head, _text(node.child_by_field_name("attribute"))]
    return None


def _model_name(node: Node | None) -> str | None:
    """`Order` / `models.Order` when the node is exactly a class-like name."""
    parts = _dotted(node)
    return ".".join(parts) if parts and _classlike(parts[-1]) and not any(_classlike(p) for p in parts[:-1]) else None


def _models_in(node: Node) -> list[str]:
    """Models named by an expression: itself, or the base of each column expression (Order.id) inside it."""
    found: list[str] = []
    stack = [node]
    while stack:
        current = stack.pop()
        parts = _dotted(current)
        if parts is not None:
            first = next((i for i, p in enumerate(parts) if _classlike(p)), None)
            if first is not None:
                found.append(".".join(parts[: first + 1]))
            continue
        if current.type == "keyword_argument":
            value = current.child_by_field_name("value")
            stack.extend([value] if value is not None else [])
        elif current.type == "call":  # func.count(Order.id): look at the arguments, not the callee
            arguments = current.child_by_field_name("arguments")
            stack.extend(reversed(arguments.named_children if arguments is not None else []))
        elif current.type != "string":
            stack.extend(reversed(current.named_children))
    return found


def _is_dynamic_string(node: Node | None) -> bool:
    """An f-string with interpolation, a +/% expression over a string, or "...".format(...)."""
    if node is None:
        return False
    if node.type == "parenthesized_expression":
        return any(_is_dynamic_string(c) for c in node.named_children)
    if node.type == "string":
        return any(c.type == "interpolation" for c in node.children)
    if node.type == "concatenated_string":
        return any(_is_dynamic_string(c) for c in node.named_children)
    if node.type == "binary_operator":
        if _text(node.child_by_field_name("operator")) not in ("+", "%"):
            return False
        return any(_has_string(node.child_by_field_name(side)) for side in ("left", "right"))
    if node.type == "call":
        function = node.child_by_field_name("function")
        return function is not None and function.type == "attribute" and _text(function.child_by_field_name("attribute")) == "format" and _has_string(function.child_by_field_name("object"))
    return False


def _has_string(node: Node | None) -> bool:
    if node is None:
        return False
    if node.type in ("string", "concatenated_string"):
        return True
    if node.type in ("binary_operator", "parenthesized_expression"):
        return any(_has_string(c) for c in node.named_children)
    return False


READ_CALLS = {"select", "query", "select_from"}
MODEL_FACTORIES = {"model_validate", "from_orm"}
WRITE_CALLS = {"insert", "update", "delete"}
PERSIST_CALLS = {"add", "add_all", "merge"}
SQL_CALLS = {"text", "execute", "exec_driver_sql"}


class PythonSource:
    id = "python.source"
    version = "1.4.0"

    def __init__(self) -> None:
        self._parser = Parser(_LANGUAGE)

    def matches(self, path: str) -> bool:
        return path.endswith(".py")

    def extract(self, path: str, data: bytes, blob_sha256: str) -> Extraction:
        out = Extraction()
        try:
            root = self._parser.parse(data).root_node
        except Exception as exc:  # the parser refusing the input must not fail the compile
            return Extraction(diagnostics=[diagnostic(self.id, "warning", f"unparseable: {type(exc).__name__}")], parsed=False)
        if root.has_error:
            out.parsed = False
            out.diagnostics.append(diagnostic(self.id, "warning", "syntax errors: statements containing them were skipped"))
        # Only statements that parsed cleanly are trusted.
        statements = [s for s in root.named_children if s.type != "comment" and not s.has_error]
        run = _Run(self.id, data, blob_sha256, out)
        run.imports(statements)
        run.symbols(statements)
        run.module_assignments(statements)
        nodes = [n for s in statements for n in _walk(s)]
        if run.fastapi:
            run.routes(nodes)
        run.models(nodes)
        if run.alembic_op:
            run.migration(statements, nodes)
        run.config_keys(nodes)
        run.string_constants(statements)
        if run.messaging:
            run.topic_uses(nodes)
        base = posixpath.basename(path)
        is_test_file = base.startswith("test_") or base.endswith("_test.py")
        for name, definition in _functions(statements):
            run.orm_uses(name, definition)
            run.dynamic_sql(name, definition)
            if is_test_file and _last(name).startswith("test_") and ("." not in name or name.startswith("Test")):
                run.test(name, definition)
        return out


class _Run:
    """State for one file."""

    def __init__(self, extractor: str, data: bytes, blob: str, out: Extraction) -> None:
        self.extractor = extractor
        self.data = data
        self.blob = blob
        self.out = out
        self.fastapi = False
        self.sqlmodel = False
        self.fastapi_names: dict[str, str] = {}  # local alias -> FastAPI / APIRouter
        self.alembic_op: set[str] = set()  # local names bound to alembic.op
        self.os_modules: set[str] = set()  # local names bound to the os module
        self.environ_names: set[str] = set()  # local names bound to os.environ
        self.getenv_names: set[str] = set()  # local names bound to os.getenv
        self.assigned_from: dict[str, str] = {}  # module-level name -> callee it was built from
        self.router_prefix: dict[str, str | None] = {}  # router name -> literal prefix (None: not literal)
        self.messaging: str | None = None  # transport, once a message-broker client is imported

    def at(self, node: Node):
        return span(self.data, node.start_byte, node.end_byte)

    def emit(self, observation) -> None:
        self.out.observations.append(observation)

    def note(self, severity: str, message: str) -> None:
        self.out.diagnostics.append(diagnostic(self.extractor, severity, message))

    # -- imports and symbols (linker inputs) ---------------------------------

    def imports(self, statements: list[Node]) -> None:
        for statement in statements:
            for node in _walk(statement):
                if node.type == "import_statement":
                    for item in node.children_by_field_name("name"):
                        module, alias = (_text(item.child_by_field_name("name")), _text(item.child_by_field_name("alias"))) if item.type == "aliased_import" else (_text(item), None)
                        self._bind_module(module, alias)
                        payload: dict[str, Any] = {"module": module, "names": [], "level": 0}
                        if alias:
                            payload["alias"] = alias
                        self.emit(obs("import", self.blob, self.at(node), payload, ast_node_kind=node.type))
                elif node.type == "import_from_statement":
                    source = node.child_by_field_name("module_name")
                    level, module = 0, _text(source)
                    if source is not None and source.type == "relative_import":
                        prefix = next((c for c in source.children if c.type == "import_prefix"), None)
                        level = _text(prefix).count(".")
                        module = _text(next((c for c in source.children if c.type == "dotted_name"), None))
                    names: list[str] = []
                    aliases: dict[str, str] = {}
                    for item in node.children_by_field_name("name"):
                        if item.type == "aliased_import":
                            name = _text(item.child_by_field_name("name"))
                            aliases[name] = _text(item.child_by_field_name("alias"))
                        else:
                            name = _text(item)
                        names.append(name)
                    if any(c.type == "wildcard_import" for c in node.children):
                        names.append("*")
                    if level == 0:
                        self._bind_names(module, names, aliases)
                    payload = {"module": module, "names": names, "level": level}
                    if aliases:
                        payload["aliases"] = aliases
                    self.emit(obs("import", self.blob, self.at(node), payload, ast_node_kind=node.type))

    def _bind_module(self, module: str, alias: str | None) -> None:
        self.messaging = self.messaging or MESSAGING_MODULES.get(module.split(".")[0])
        if module == "fastapi" or module.startswith("fastapi."):
            self.fastapi = True
        if module == "sqlmodel" or module.startswith("sqlmodel."):
            self.sqlmodel = True
        if module == "os":
            self.os_modules.add(alias or "os")
        if module == "alembic.op" and alias:
            self.alembic_op.add(alias)

    def _bind_names(self, module: str, names: list[str], aliases: dict[str, str]) -> None:
        self.messaging = self.messaging or MESSAGING_MODULES.get(module.split(".")[0])
        if module == "fastapi" or module.startswith("fastapi."):
            self.fastapi = True
            for name in names:
                if name in FASTAPI_FACTORIES:
                    self.fastapi_names[aliases.get(name, name)] = name
        if module == "sqlmodel" or module.startswith("sqlmodel."):
            self.sqlmodel = True
        if module == "alembic" and "op" in names:
            self.alembic_op.add(aliases.get("op", "op"))
        if module == "os":
            if "environ" in names:
                self.environ_names.add(aliases.get("environ", "environ"))
            if "getenv" in names:
                self.getenv_names.add(aliases.get("getenv", "getenv"))

    def symbols(self, statements: list[Node]) -> None:
        for statement in statements:
            definition = _definition(statement)
            kind = {"function_definition": "function", "class_definition": "class"}.get(definition.type)
            name = definition.child_by_field_name("name")
            if kind and name is not None:
                self.emit(obs("symbol", self.blob, self.at(name), {"name": _text(name), "symbol_kind": kind}, ast_node_kind=definition.type))

    def module_assignments(self, statements: list[Node]) -> None:
        for statement in statements:
            assignment = _assignment(statement)
            if assignment is None:
                continue
            left, right = assignment.child_by_field_name("left"), assignment.child_by_field_name("right")
            if left is None or left.type != "identifier" or right is None or right.type != "call":
                continue
            callee = _callee(right)
            self.assigned_from[_text(left)] = callee
            if self._factory(callee) == "APIRouter":
                prefix = _args(right)[1].get("prefix")
                self.router_prefix[_text(left)] = "" if prefix is None else _literal(prefix)

    # -- event topics ------------------------------------------------------------

    def string_constants(self, statements: list[Node]) -> None:
        """Module-level `NAME = "value"` where the value could be a topic name.

        Kept for the linker, which resolves a topic passed by name. Names that
        read like credentials are skipped so a value that may be a secret never
        enters a payload.
        """
        for statement in statements:
            assignment = _assignment(statement)
            if assignment is None:
                continue
            left, value = assignment.child_by_field_name("left"), _literal(assignment.child_by_field_name("right"))
            if left is None or left.type != "identifier" or value is None:
                continue
            name = _text(left)
            if _TOPIC_NAME.match(value) and not _SECRET_LIKE.search(name):
                self.emit(obs("string_constant", self.blob, self.at(assignment), {"name": name, "value": value}, ast_node_kind="assignment"))

    def topic_uses(self, nodes: list[Node]) -> None:
        """`producer.produce(topic, ...)` and `consumer.subscribe([topic, ...])`.

        A topic is recorded as a literal, as a name for the linker to resolve,
        or as dynamic when it is computed at run time.
        """
        for call in nodes:
            if call.type != "call":
                continue
            function = call.child_by_field_name("function")
            if function is None or function.type != "attribute":
                continue
            method = _text(function.child_by_field_name("attribute"))
            role = "publish" if method in PUBLISH_CALLS else "subscribe" if method in SUBSCRIBE_CALLS else None
            if role is None:
                continue
            positional, keywords = _args(call)
            target = positional[0] if positional else keywords.get("topic") or keywords.get("topics")
            if target is None:
                continue
            items = list(target.named_children) if target.type in ("list", "tuple", "set") else [target]
            for item in items or [target]:
                payload: dict[str, Any] = {"role": role, "transport": self.messaging}
                literal = _literal(item)
                if literal is not None and _TOPIC_NAME.match(literal):
                    payload["topic"] = literal
                elif item.type == "identifier" or (item.type == "attribute" and _dotted(item)):
                    payload["name"] = _text(item)
                else:
                    payload["dynamic"] = True
                self.emit(obs("topic_use", self.blob, self.at(call), payload, ast_node_kind="call"))

    def _factory(self, callee: str) -> str | None:
        """"FastAPI" / "APIRouter" if `callee` constructs one, else None."""
        if callee in self.fastapi_names:
            return self.fastapi_names[callee]
        if "." in callee and _last(callee) in FASTAPI_FACTORIES and callee.split(".", 1)[0] == "fastapi":
            return _last(callee)
        return None

    # -- FastAPI routes -------------------------------------------------------

    def routes(self, nodes: list[Node]) -> None:
        for node in nodes:
            if node.type != "decorated_definition":
                continue
            definition = node.child_by_field_name("definition")
            if definition is None or definition.type != "function_definition":
                continue
            handler = _text(definition.child_by_field_name("name"))
            for decorator in (c for c in node.children if c.type == "decorator"):
                call = next((c for c in decorator.named_children if c.type == "call"), None)
                function = call.child_by_field_name("function") if call is not None else None
                if call is None or function is None or function.type != "attribute":
                    continue
                verb = _text(function.child_by_field_name("attribute"))
                owner = _text(function.child_by_field_name("object"))
                if verb not in HTTP_METHODS and verb not in MULTI_METHOD_DECORATORS:
                    continue
                if owner in self.assigned_from and self._factory(self.assigned_from[owner]) is None:
                    continue  # a local object that is visibly not a FastAPI app or router
                positional, keywords = _args(call)
                path = _literal(keywords.get("path") or (positional[0] if positional else None))
                prefix = self.router_prefix.get(owner, "")
                if path is None or prefix is None:
                    self.note("info", f"route on {handler}: path is not a string literal; no Endpoint recorded")
                    continue
                methods = [verb.upper()] if verb in HTTP_METHODS else self._methods(keywords.get("methods"))
                if methods is None:
                    self.note("info", f"route on {handler}: methods are not string literals; no Endpoint recorded")
                    continue
                route = prefix + path
                for method in methods:
                    at = self.at(decorator)
                    key = {"component_root": UNIT, "protocol": "http", "method": method, "route": route}
                    self.emit(entity(self.blob, at, EntityKind.ENDPOINT, key, f"{method} {route}", {"framework": "fastapi", "handler": handler}))
                    self.emit(obs("route_handler", self.blob, at, {"method": method, "route": route, "handler": handler}, ast_node_kind="decorator"))

    @staticmethod
    def _methods(node: Node | None) -> list[str] | None:
        if node is None:
            return ["GET"]  # FastAPI's default for api_route
        if node.type not in ("list", "tuple", "set"):
            return None
        values = [_literal(c) for c in node.named_children if c.type != "comment"]
        return None if not values or any(v is None for v in values) else [v.upper() for v in values if v]

    # -- SQLAlchemy models ----------------------------------------------------

    def models(self, nodes: list[Node]) -> None:
        classes = {_text(n.child_by_field_name("name")): n for n in nodes if n.type == "class_definition"}
        for node in nodes:
            if node.type != "class_definition":
                continue
            assignments = _class_assignments(node)
            if self.sqlmodel and _is_sqlmodel_table(node):
                self._sqlmodel_table(node, assignments, classes)
                continue
            table = table_at = None
            for statement, assignment in assignments:
                if _text(assignment.child_by_field_name("left")) == "__tablename__":
                    table, table_at = _literal(assignment.child_by_field_name("right")), statement
            if table_at is None:
                continue
            model = _text(node.child_by_field_name("name"))
            if not table:
                self.note("info", f"model {model}: __tablename__ is not a string literal; no Table recorded")
                continue
            table_ref = ref(EntityKind.TABLE, {"datastore": "default", "name": table}, table)
            self.emit(entity(self.blob, self.at(table_at), EntityKind.TABLE, table_ref["natural_key"], table, {"orm": "sqlalchemy", "model": model}))
            for statement, assignment in assignments:
                column = self._column(assignment)
                if column:
                    at = self.at(statement)
                    key = {"table": table, "name": column}
                    self.emit(entity(self.blob, at, EntityKind.FIELD, key, column, {"orm": "sqlalchemy", "attribute": _text(assignment.child_by_field_name("left"))}))
                    self.emit(relation(self.blob, at, table_ref, Predicate.CONTAINS, ref(EntityKind.FIELD, key, column)))

    def _sqlmodel_table(self, node: Node, assignments: list[tuple[Node, Node]], classes: dict[str, Node]) -> None:
        model = _text(node.child_by_field_name("name"))
        table, table_at = model.lower(), None  # SQLModel's default table name
        for statement, assignment in assignments:
            if _text(assignment.child_by_field_name("left")) == "__tablename__":
                table, table_at = _literal(assignment.child_by_field_name("right")), statement
        if not table:
            self.note("info", f"model {model}: __tablename__ is not a string literal; no Table recorded")
            return
        if table_at is not None:
            at = self.at(table_at)
        else:  # the class header: name through the base list
            name, bases = node.child_by_field_name("name"), node.child_by_field_name("superclasses")
            at = span(self.data, name.start_byte, (bases or name).end_byte)
        table_ref = ref(EntityKind.TABLE, {"datastore": "default", "name": table}, table)
        self.emit(entity(self.blob, at, EntityKind.TABLE, table_ref["natural_key"], table, {"orm": "sqlmodel", "model": model}))
        seen: set[str] = set()
        visited: set[str] = set()

        def fields(owner: str, cls: Node) -> None:
            visited.add(owner)
            for statement, assignment in _class_assignments(cls):
                column = self._sqlmodel_column(assignment)
                if column and column not in seen:
                    seen.add(column)
                    attributes = {"orm": "sqlmodel", "attribute": column, **({"inherited_from": owner} if owner != model else {})}
                    field_at = self.at(statement)
                    key = {"table": table, "name": column}
                    self.emit(entity(self.blob, field_at, EntityKind.FIELD, key, column, attributes))
                    self.emit(relation(self.blob, field_at, table_ref, Predicate.CONTAINS, ref(EntityKind.FIELD, key, column)))
            bases = cls.child_by_field_name("superclasses")
            for base in bases.named_children if bases is not None else []:
                name = _text(base)
                if base.type == "identifier" and name in classes and name not in visited:
                    fields(name, classes[name])  # bases defined elsewhere are left to the linker

        fields(model, node)

    @staticmethod
    def _sqlmodel_column(assignment: Node) -> str | None:
        """An annotated class attribute that is not a Relationship(), a ClassVar or a dunder."""
        left, right, annotation = (assignment.child_by_field_name(f) for f in ("left", "right", "type"))
        if left is None or left.type != "identifier" or annotation is None or _text(left).startswith("__"):
            return None
        if _last(_callee(right)) == "Relationship" or _last(_text(annotation).split("[", 1)[0].strip()) == "ClassVar":
            return None
        return _text(left)

    @staticmethod
    def _column(assignment: Node) -> str | None:
        """Column name declared by a class-body assignment, or None if it is not a column."""
        left = assignment.child_by_field_name("left")
        right = assignment.child_by_field_name("right")
        annotation = assignment.child_by_field_name("type")
        if left is None or left.type != "identifier" or _text(left).startswith("__"):
            return None
        call = _last(_callee(right))
        if call in NON_COLUMN_CALLS:
            return None
        mapped = annotation is not None and _text(annotation).replace(" ", "").split("[", 1)[0].rsplit(".", 1)[-1] == "Mapped"
        if call in COLUMN_CALLS:
            positional = _args(right)[0]  # an explicit first string argument overrides the attribute name
            return (_literal(positional[0]) if positional else None) or _text(left)
        return _text(left) if mapped and (right is None or right.type != "call") else None

    # -- Alembic migrations ---------------------------------------------------

    def migration(self, statements: list[Node], nodes: list[Node]) -> None:
        revision = revision_at = None
        down: Any = None
        for statement in statements:
            assignment = _assignment(statement)
            if assignment is None:
                continue
            name, right = _text(assignment.child_by_field_name("left")), assignment.child_by_field_name("right")
            if name == "revision":
                revision, revision_at = _literal(right), statement
            elif name == "down_revision" and right is not None:
                if right.type in ("tuple", "list"):
                    down = [v for v in (_literal(c) for c in right.named_children) if v is not None]
                else:
                    down = _literal(right)
        if revision_at is None or not revision:
            return
        me = ref(EntityKind.MIGRATION, {"tool": "alembic", "revision": revision}, revision)
        self.emit(entity(self.blob, self.at(revision_at), EntityKind.MIGRATION, me["natural_key"], revision, {"down_revision": down}))
        for node in nodes:
            callee = _callee(node)
            if "." not in callee or callee.rsplit(".", 1)[0] not in self.alembic_op:
                continue
            operation = _last(callee)
            positional, keywords = _args(node)
            at = self.at(node)
            table = _literal(positional[0] if positional else keywords.get("table_name"))
            if operation not in TABLE_OPS and operation not in ("add_column", "alter_column", "drop_column"):
                continue
            if not table:
                self.note("info", f"alembic {operation}: table name is not a string literal; not recorded")
                continue
            # Dropping something must not declare it: refer without declaring.
            table_ref = ref(EntityKind.TABLE, {"datastore": "default", "name": table}, table, declare=operation != "drop_table")
            column: str | None = None
            declared: str | None = None  # the Field that exists after this operation
            if operation == "add_column":
                spec = positional[1] if len(positional) > 1 else keywords.get("column")
                spec_args = _args(spec)[0] if spec is not None and spec.type == "call" else []
                column = declared = _literal(spec_args[0]) if spec_args else None
            elif operation in ("alter_column", "drop_column"):
                column = _literal(positional[1] if len(positional) > 1 else keywords.get("column_name"))
                if operation == "alter_column":
                    renamed = keywords.get("new_column_name")
                    declared = _literal(renamed) if renamed is not None else column
            qualifiers = {"op": operation, **({"column": column} if column and operation != "add_column" else {})}
            self.emit(relation(self.blob, at, me, Predicate.MIGRATES, table_ref, qualifiers))
            if declared:
                key = {"table": table, "name": declared}
                self.emit(entity(self.blob, at, EntityKind.FIELD, key, declared))
                self.emit(relation(self.blob, at, me, Predicate.MIGRATES, ref(EntityKind.FIELD, key, declared), qualifiers))

    # -- linker inputs: ORM use, dynamic SQL, tests ----------------------------

    def orm_uses(self, function: str, definition: Node) -> None:
        body = definition.child_by_field_name("body")
        calls = [n for n in _walk(body) if n.type == "call"] if body is not None else []
        seen: set[tuple[str, str]] = set()

        def record(model: str | None, access: str, call: Node) -> None:
            if model and (model, access) not in seen:
                seen.add((model, access))
                self.emit(obs("orm_use", self.blob, self.at(call), {"function": function, "model": model, "access": access}, ast_node_kind="call"))

        # Variables whose model is evident in this function: annotated parameters and
        # assignments from Model(...), Model.model_validate(...), session.get(Model, ...).
        typed: dict[str, str] = {}
        parameters = definition.child_by_field_name("parameters")
        for parameter in parameters.named_children if parameters is not None else []:
            if parameter.type in ("typed_parameter", "typed_default_parameter"):
                name = parameter.child_by_field_name("name") or next((c for c in parameter.named_children if c.type == "identifier"), None)
                model = _annotated_model(parameter.child_by_field_name("type"))
                if name is not None and model:
                    typed[_text(name)] = model
        for node in _walk(body) if body is not None else []:
            if node.type == "assignment":
                left = node.child_by_field_name("left")
                model = _built_model(node.child_by_field_name("right")) or _annotated_model(node.child_by_field_name("type"))
                if left is not None and left.type == "identifier" and model:
                    typed[_text(left)] = model

        persisted_names: set[str] = set()  # variables handed to session.add / add_all / merge
        persisted_calls: set[int] = set()  # constructor calls written directly inside those arguments
        for call in calls:
            target = call.child_by_field_name("function")
            if target is not None and target.type == "attribute" and _text(target.child_by_field_name("attribute")) in PERSIST_CALLS:
                arguments = call.child_by_field_name("arguments")
                for node in _walk(arguments) if arguments is not None else []:
                    if node.type == "identifier":
                        persisted_names.add(_text(node))
                    elif node.type == "call":
                        persisted_calls.add(node.id)
        for call in calls:
            target = call.child_by_field_name("function")
            verb = _last(_text(target))
            is_method = target is not None and target.type == "attribute"
            positional, _ = _args(call)
            if verb in READ_CALLS and (verb == "select" or is_method):
                for argument in positional:
                    for model in _models_in(argument):
                        record(model, "read", call)
            elif verb == "get" and is_method and len(positional) >= 2:
                record(_model_name(positional[0]), "read", call)
            elif verb in WRITE_CALLS and positional and _model_name(positional[0]) is not None:
                record(_model_name(positional[0]), "write", call)
            elif is_method and (verb in PERSIST_CALLS or verb == "delete"):
                # session.add(obj) / session.delete(obj): attributable when obj's model is evident here.
                arguments = call.child_by_field_name("arguments")
                for node in _walk(arguments) if arguments is not None else []:
                    if node.type == "identifier" and _text(node) in typed and (node.parent is None or node.parent.type not in ("attribute", "keyword_argument", "call")):
                        record(typed[_text(node)], "write", call)
            elif _model_name(target) is not None or (is_method and verb in MODEL_FACTORIES and _model_name(target.child_by_field_name("object")) is not None):
                parent = call.parent
                assigned = _text(parent.child_by_field_name("left")) if parent is not None and parent.type == "assignment" and parent.child_by_field_name("right") == call else None
                if call.id in persisted_calls or (assigned and assigned in persisted_names):
                    record(_built_model(call), "write", call)

    def dynamic_sql(self, function: str, definition: Node) -> None:
        body = definition.child_by_field_name("body")
        for call in _walk(body) if body is not None else []:
            if call.type != "call" or _last(_callee(call)) not in SQL_CALLS:
                continue
            positional, _ = _args(call)
            if positional and _is_dynamic_string(positional[0]):
                self.emit(obs("dynamic_sql", self.blob, self.at(call), {"function": function}, ast_node_kind="call"))

    def test(self, name: str, definition: Node) -> None:
        start = definition.child_by_field_name("name")
        end = definition.child_by_field_name("parameters") or start
        if start is not None and end is not None:
            self.emit(obs("test", self.blob, span(self.data, start.start_byte, end.end_byte), {"name": name}, ast_node_kind="function_definition"))

    # -- configuration keys (names only) --------------------------------------

    def config_keys(self, nodes: list[Node]) -> None:
        seen: set[str] = set()
        for node in nodes:
            name = None
            end = node.end_byte
            if node.type == "subscript" and self._is_environ(node.child_by_field_name("value")):
                keys = node.children_by_field_name("subscript")
                name = _literal(keys[0]) if len(keys) == 1 else None
            elif node.type == "call":
                function = node.child_by_field_name("function")
                via_get = function is not None and function.type == "attribute" and _text(function.child_by_field_name("attribute")) == "get" and self._is_environ(function.child_by_field_name("object"))
                if via_get or self._is_getenv(function):
                    positional, keywords = _args(node)
                    key_node = positional[0] if positional else keywords.get("key")
                    name = _literal(key_node)
                    end = key_node.end_byte if key_node is not None else end  # stop before any default value
            if not name or name in seen:
                continue
            seen.add(name)
            at = span(self.data, node.start_byte, end)
            key = {"name": name}
            self.emit(entity(self.blob, at, EntityKind.CONFIG_KEY, key, name))
            self.emit(relation(self.blob, at, PACKAGE, Predicate.REFERENCES_CONFIG, ref(EntityKind.CONFIG_KEY, key, name)))

    def _is_environ(self, node: Node | None) -> bool:
        if node is None:
            return False
        if node.type == "identifier":
            return _text(node) in self.environ_names
        return node.type == "attribute" and _text(node.child_by_field_name("attribute")) == "environ" and _text(node.child_by_field_name("object")) in self.os_modules

    def _is_getenv(self, node: Node | None) -> bool:
        if node is None:
            return False
        if node.type == "identifier":
            return _text(node) in self.getenv_names
        return node.type == "attribute" and _text(node.child_by_field_name("attribute")) == "getenv" and _text(node.child_by_field_name("object")) in self.os_modules


EXTRACTORS = [PythonSource()]
