"""Form models: the declared schemas a form's bindings can reach (docs/solution-ir-contract.md, section 8.5).

A binding binds a `Field` only when the form's model type is a declared schema whose
fields are literal, in the same file:

  TypeScript   `interface NewOrder { customer: string; ... }`, `type NewOrder = { ... }`
  Zod          `const orderSchema = z.object({ customer: z.string(), ... })`
  Yup          `const orderSchema = Yup.object({ ... })`, `yup.object().shape({ ... })`

A schema is not literal when part of its field list comes from elsewhere: an interface
that `extends` another, an index signature, an intersection or mapped type, a spread, a
Zod `.extend()`, `.merge()`, `.pick()` or `.omit()`. Such a schema binds nothing.

The model of a form comes from what this file shows:

  react-hook-form   `useForm<NewOrder>()`, or `useForm({ resolver: zodResolver(orderSchema) })`,
                    for the controls bound with `register("x")`
  Formik            `<Formik validationSchema={orderSchema}>` around the form, for `name="x"`
  any form          a `validationSchema` (`:validation-schema`) on the form element, for `name="x"`
  typed variables   `const form = reactive<CheckoutForm>(...)`, `const form: CheckoutForm = ...`,
                    a class field `model: UserForm`, for `v-model="form.email"`,
                    `[(ngModel)]="model.email"` and other bindings whose path starts with it

A field is `Field {table: "<file path>#<schema name>", name: "<field path>"}`, nested
object fields named by a dotted path. Nothing is run; names only, never default values.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from repo_intel.platforms.web import script
from repo_intel.platforms.web.markup import Element

_ZOD_CHAIN = frozenset({"strict", "strip", "passthrough", "refine", "superRefine", "describe", "partial", "required", "deepPartial",
                        "optional", "nullable", "nullish", "default", "brand", "readonly", "transform", "catch"})
_YUP_CHAIN = frozenset({"required", "noUnknown", "strict", "test", "label", "defined", "nullable", "optional", "default", "meta", "camelCase"})
_RESOLVERS = frozenset({"zodResolver", "yupResolver", "toTypedSchema", "toFormValidator"})
_MAX_DEPTH = 4
_PATH = re.compile(r"[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*")


@dataclass
class Schema:
    name: str  # the type name or the schema variable's name
    kind: str  # typescript-interface | typescript-type | zod | yup
    start: int
    end: int
    literal: bool = True
    fields: dict[str, tuple[int, int]] = field(default_factory=dict)  # dotted field path -> its span


_TYPED_CALLS = frozenset({"reactive", "ref", "shallowRef", "shallowReactive", "signal", "toRef"})
_MODEL_BINDINGS = frozenset({"v-model", "ngModel", "formControl", "value-binding"})


@dataclass
class Reach:
    """Where one control's binding leads: a field of a schema, or why it leads nowhere."""

    model: str  # the schema or type name the form uses
    field: str  # the binding path relative to that model
    schema: Schema | None  # None when this file declares no schema of that name
    start: int  # where the model is named, for the construct's span
    end: int


class Models:
    """The schemas one file declares and which of them its forms use as models."""

    def __init__(self, scripts: list[script.Script]):
        self.schemas: dict[str, Schema] = {}
        self.typed: dict[str, tuple[str, int, int]] = {}  # variable or class field -> (type name, span)
        self.register: list[tuple[str | None, int, int]] = []  # each react-hook-form useForm: (its model, span)
        for s in scripts:
            for name, found in schemas(s).items():
                self.schemas.setdefault(name, found)
            self._read(s)

    def _read(self, s: script.Script) -> None:
        for n in s.nodes:
            if n.type == "call_expression" and script.tail_name(n.child_by_field_name("function")) == "useForm":
                fn = script.unwrap(n.child_by_field_name("function"))
                bound = s.imports.get(script.text(fn)) if fn is not None and fn.type == "identifier" else None
                if bound is not None and bound[0] == "react-hook-form":
                    self.register.append((_use_form_model(n), *s.at(n)))
            elif n.type == "variable_declarator":
                name = n.child_by_field_name("name")
                if name is None or name.type != "identifier":
                    continue
                typ = _simple_type(n.child_by_field_name("type"))
                value = script.unwrap(n.child_by_field_name("value"))
                if typ is None and value is not None and value.type == "call_expression" and script.tail_name(value.child_by_field_name("function")) in _TYPED_CALLS:
                    typ = _type_argument(value)
                if typ is not None:
                    self.typed.setdefault(script.text(name), (typ, *s.at(n)))
            elif n.type in ("public_field_definition", "field_definition"):
                name = n.child_by_field_name("name") or n.child_by_field_name("property")
                if name is None or name.type != "property_identifier":
                    continue
                typ = _simple_type(n.child_by_field_name("type"))
                value = n.child_by_field_name("value")
                if typ is None and value is not None and value.type == "as_expression":
                    typ = next((script.text(c) for c in value.named_children[1:] if c.type == "type_identifier"), None)
                if typ is not None:
                    self.typed.setdefault(script.text(name), (typ, *s.at(n)))

    def reach(self, el: Element, binds: str, how: str) -> Reach | None:
        """The model field a control's binding reaches, or None when the form has no model
        this file names. `how` is the binding's source: `register`, a model binding
        (`v-model`, `ngModel`, ...), `name`, `formControlName`."""
        if not _PATH.fullmatch(binds):
            return None
        if how == "register":
            if len(self.register) != 1 or self.register[0][0] is None:
                return None  # no useForm, several (which one registers it is not read), or one without a model
            model, a, b = self.register[0]
            return Reach(model, binds, self.schemas.get(model), a, b)
        if how in _MODEL_BINDINGS:
            head, _, rest = binds.partition(".")
            if not rest or head not in self.typed:
                return None
            model, a, b = self.typed[head]
            return Reach(model, rest, self.schemas.get(model), a, b)
        if how == "name":  # a validation schema on the form, or on Formik around it
            for holder in el.ancestors():
                attr = holder.get("validationSchema", "validation-schema")
                if attr is not None and not attr.literal and attr.value and re.fullmatch(r"[A-Za-z_$][\w$]*", attr.value.strip()):
                    model = attr.value.strip()
                    return Reach(model, binds, self.schemas.get(model), attr.start, attr.end)
        return None


def _use_form_model(call) -> str | None:
    """`useForm<NewOrder>()` -> NewOrder; `useForm({ resolver: zodResolver(orderSchema) })` -> orderSchema."""
    typ = _type_argument(call)
    if typ is not None:
        return typ
    args = call.child_by_field_name("arguments")
    first = script.unwrap(args.named_children[0]) if args is not None and args.named_children else None
    resolver = script.unwrap(script.pairs(first).get("resolver")) if first is not None else None
    if resolver is not None and resolver.type == "call_expression" and script.tail_name(resolver.child_by_field_name("function")) in _RESOLVERS:
        inner = resolver.child_by_field_name("arguments")
        schema = script.unwrap(inner.named_children[0]) if inner is not None and inner.named_children else None
        if schema is not None and schema.type == "identifier":
            return script.text(schema)
    return None


def _type_argument(call) -> str | None:
    """The single plain type argument of a call (`f<T>()`), else None."""
    args = call.child_by_field_name("type_arguments")
    if args is None or len(args.named_children) != 1:
        return None
    only = args.named_children[0]
    return script.text(only) if only.type == "type_identifier" else None


def _simple_type(annotation) -> str | None:
    """`: T` -> T, for a plain type name only (not an array, union or generic)."""
    if annotation is None or not annotation.named_children:
        return None
    only = annotation.named_children[0]
    return script.text(only) if only.type == "type_identifier" else None


def schemas(s: script.Script) -> dict[str, Schema]:
    """Every declared schema in one script, by name (the first declaration of a name wins)."""
    out: dict[str, Schema] = {}
    for n in s.nodes:
        found: Schema | None = None
        if n.type == "interface_declaration":
            name, body = n.child_by_field_name("name"), n.child_by_field_name("body")
            if name is None or body is None:
                continue
            found = Schema(script.text(name), "typescript-interface", *s.at(n))
            found.literal = not any(c.type == "extends_type_clause" for c in n.children) and _type_fields(s, body, "", found.fields, 0)
        elif n.type == "type_alias_declaration":
            name, value = n.child_by_field_name("name"), n.child_by_field_name("value")
            if name is None or value is None:
                continue
            found = Schema(script.text(name), "typescript-type", *s.at(n))
            found.literal = value.type == "object_type" and _type_fields(s, value, "", found.fields, 0)
        elif n.type == "variable_declarator":
            name, value = n.child_by_field_name("name"), script.unwrap(n.child_by_field_name("value"))
            if name is None or name.type != "identifier" or value is None or value.type != "call_expression":
                continue
            found = _object_schema(s, script.text(name), value, n)
        if found is not None:
            out.setdefault(found.name, found)
    return out


def _type_fields(s: script.Script, body, prefix: str, fields: dict[str, tuple[int, int]], depth: int) -> bool:
    """Fields of an interface body or object type; False when a member is not a literal field."""
    literal = True
    for m in body.named_children:
        if m.type == "property_signature":
            key = m.child_by_field_name("name")
            name = _key(key)
            if name is None:
                literal = False
                continue
            path = prefix + name
            fields[path] = s.at(m)
            annotation = m.child_by_field_name("type")
            inner = annotation.named_children[0] if annotation is not None and annotation.named_children else None
            if inner is not None and inner.type == "object_type" and depth < _MAX_DEPTH:
                literal = _type_fields(s, inner, path + ".", fields, depth + 1) and literal
        elif m.type in ("index_signature", "call_signature", "construct_signature"):
            literal = False
        # method signatures are behaviour, not fields
    return literal


def _key(node) -> str | None:
    if node is None:
        return None
    if node.type in ("property_identifier", "identifier"):
        return script.text(node)
    if node.type == "string":
        return script.string_value(node)
    return None


def _object_schema(s: script.Script, name: str, value, decl) -> Schema | None:
    """A Zod or Yup object schema assigned to `name`, else None."""
    outer: list[tuple[str, Any]] = []  # (method, its first argument), from the outermost call inwards
    node = value
    while node is not None and node.type == "call_expression":
        fn = script.unwrap(node.child_by_field_name("function"))
        if fn is None:
            return None
        args = node.child_by_field_name("arguments")
        first = script.unwrap(args.named_children[0]) if args is not None and args.named_children else None
        library = _library(s, fn)
        if library is not None and script.tail_name(fn) == "object":
            shapes = [arg for method, arg in outer if method == "shape"]
            others = [method for method, _ in outer if method != "shape"]
            allowed = _ZOD_CHAIN if library == "zod" else _YUP_CHAIN
            # Yup takes the fields in object({...}) or in one .shape({...}) after object().
            target = first if first is not None else (shapes[0] if library == "yup" and len(shapes) == 1 else None)
            found = Schema(name, library, *s.at(decl))
            found.literal = (target is not None and target.type == "object" and all(m in allowed for m in others)
                             and len(shapes) == (0 if first is not None else 1))
            if target is not None and target.type == "object":
                found.literal = _object_fields(s, target, library, "", found.fields, 0) and found.literal
            return found
        if fn.type != "member_expression":
            return None
        outer.append((script.tail_name(fn) or "", first))
        node = script.unwrap(fn.child_by_field_name("object"))
    return None


def _library(s: script.Script, fn) -> str | None:
    """'zod' or 'yup' for `z.object`, `Yup.object`, `yup.object` or an `object` imported from either."""
    if fn.type == "member_expression":
        root = script.unwrap(fn.child_by_field_name("object"))
        if root is None or root.type != "identifier":
            return None
        bound = s.imports.get(script.text(root))
        spec = bound[0] if bound is not None else None
    elif fn.type == "identifier":
        bound = s.imports.get(script.text(fn))
        spec = bound[0] if bound is not None and bound[1] == "object" else None
    else:
        return None
    return {"zod": "zod", "yup": "yup"}.get(spec or "")


def _object_fields(s: script.Script, obj, library: str, prefix: str, fields: dict[str, tuple[int, int]], depth: int) -> bool:
    literal = True
    for p in obj.named_children:
        if p.type != "pair":
            literal = False  # a spread or a shorthand: the field list is not all here
            continue
        name = _key(p.child_by_field_name("key"))
        if name is None:
            literal = False
            continue
        path = prefix + name
        fields[path] = s.at(p)
        value = script.unwrap(p.child_by_field_name("value"))
        nested = _nested_object(s, value, library)
        if nested is not None and depth < _MAX_DEPTH:
            literal = _object_fields(s, nested, library, path + ".", fields, depth + 1) and literal
    return literal


def _nested_object(s: script.Script, value, library: str):
    """The object literal of a nested `z.object({...})` or `yup.object({...})` field, if it is one."""
    node = value
    while node is not None and node.type == "call_expression":
        fn = script.unwrap(node.child_by_field_name("function"))
        if fn is None:
            return None
        if _library(s, fn) == library and script.tail_name(fn) == "object":
            args = node.child_by_field_name("arguments")
            first = script.unwrap(args.named_children[0]) if args is not None and args.named_children else None
            return first if first is not None and first.type == "object" else None
        node = script.unwrap(fn.child_by_field_name("object")) if fn.type == "member_expression" else None
    return None
