"""`hadoop.oozie`: Oozie workflow, coordinator and bundle definitions as Jobs.

Recognised by the root element, whatever the file is called:

    <workflow-app name=...>     a Job (`hdp:OozieWorkflow`, framework `oozie`, named by its path)
      <action name=...>           a Task (`hdp:OozieAction`) the Job CONTAINS; its type in attributes
        <hive|pig> <script>         RUNS the script's Program when the path is in the application
        <sqoop> <command>|<arg>     the Task READS_FROM and WRITES_TO the table and path it moves
        <shell> <exec>, <spark>     RUNS the file (an Artifact) when its path is in the application;
                                    a shell exec (.sh .ksh .bash .bat .cmd .ps1) also RUNS the
                                    Program {shell, path} the shell pack declares
        <distcp> <arg>              READS_FROM its sources and WRITES_TO its target
        <sub-workflow> <app-path>   RUNS that workflow's Job when the path is literal
        <ok to=> <error to=>        transitions: the next action DEPENDS_ON this one
      <decision> <fork> <join>    control nodes, followed through to the actions they lead to
      <kill> <end>
    <coordinator-app>           a Job (`hdp:OozieCoordinator`) with its schedule, reading and writing
                                its datasets (uri-templates) and RUNS the workflow it starts
    <bundle-app>                a Job (`hdp:OozieBundle`) that RUNS its coordinators

A dependency runs from the downstream action to the upstream one, with qualifiers `on` (ok or
error) and `via` (the control nodes passed, such as `fork` or `decision`). The workflow's
order is also one behaviour unit (kind `flow`) owned by the Job: each action is a `try` whose
body calls the task and takes the ok transition, with the error transition in its catch; a
decision is `if`/`else` on its EL predicate; fork and join are `platform` nodes; kill is
`throw`; end is `exit`.

Names never values: parameter and configuration names are kept, their values and defaults
never; credentials, connection strings, passwords, email recipients and ssh hosts are never
read. A path built from a parameter keeps it as `{name}` and is marked parameterized; one the
file does not state at all (`${appPath}/hive/x.hql` with no literal part) is left unresolved
with a diagnostic. A file with a DOCTYPE or entity declarations is not read.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field
from typing import Any

from repo_intel.compile.ontology import EntityKind as K
from repo_intel.compile.ontology import EvidenceClass
from repo_intel.platforms.hadoop.common import E1, P, Out, dataset_path, placeholder, program, resolve_relative
from repo_intel.platforms.hadoop.vocabulary import ACTION_CONSTRUCTS

EXTRACTOR = "hadoop.oozie"
FRAMEWORK = "oozie"
_ROOT = re.compile(rb"<(?:[\w.-]+:)?(workflow-app|coordinator-app|bundle-app)[\s>/]")
_UNSAFE = re.compile(rb"<!(?:DOCTYPE|ENTITY)", re.I)
_APP_PATH = re.compile(r"^\$\{\s*wf:appPath\(\)\s*\}/?")
_CONTROL = ("start", "decision", "fork", "join", "kill", "end")
_FREQ = re.compile(r"^\$\{\s*coord:(minutes|hours|days|months|endOfDays|endOfMonths)\(\s*(\d+)\s*\)\s*\}$")
SHELL_SUFFIXES = (".sh", ".ksh", ".bash", ".bat", ".cmd", ".ps1")  # scripts the shell pack declares as Programs
_UNITS = {"minutes": "minute", "hours": "hour", "days": "day", "months": "month", "endOfDays": "day", "endOfMonths": "month"}


class OozieDefinitions:
    id = EXTRACTOR
    version = "1.1.0"

    def matches(self, path: str) -> bool:
        return path.lower().endswith(".xml")

    def extract(self, path: str, data: bytes, blob: str):
        from repo_intel.extractors import base

        head = _ROOT.search(data[:4096])
        if head is None:
            return base.Extraction(parsed=False)
        if _UNSAFE.search(data):
            return base.Extraction(diagnostics=[base.diagnostic(self.id, "warning", "an Oozie definition with a DOCTYPE or entity declarations is not read")], parsed=False)
        try:
            root = parse_tree(data)
        except ValueError as e:
            return base.Extraction(diagnostics=[base.diagnostic(self.id, "warning", f"not well-formed XML ({e})")], parsed=False)
        if root.tag not in ("workflow-app", "coordinator-app", "bundle-app"):
            return base.Extraction(parsed=False)
        out = Out(EXTRACTOR, path, data, blob, evidence_class=E1)
        if root.tag == "workflow-app":
            _Workflow(out, path, data, root).run()
        elif root.tag == "coordinator-app":
            _Coordinator(out, path, data, root).run()
        else:
            _Bundle(out, path, data, root).run()
        return out.finish()


# ------------------------------------------------------------------ the tree


@dataclass
class Node:
    tag: str
    attrs: dict[str, str]
    start: int  # byte offsets of the element in the file
    end: int = 0
    inner: tuple[int, int] = (0, 0)  # byte offsets of the content between the tags
    text: str = ""
    children: list[Node] = field(default_factory=list)

    def child(self, tag: str) -> Node | None:
        return next((c for c in self.children if c.tag == tag), None)

    def all(self, tag: str) -> list[Node]:
        return [c for c in self.children if c.tag == tag]

    def text_of(self, tag: str) -> str | None:
        c = self.child(tag)
        return c.text.strip() if c is not None and c.text.strip() else None


def parse_tree(data: bytes) -> Node:
    """The element tree with exact byte offsets (namespace prefixes dropped from tags)."""
    from xml.parsers import expat

    p = expat.ParserCreate()
    p.buffer_text = True
    stack: list[Node] = []
    top: list[Node] = []
    texts: list[list[str]] = []

    def local(name: str) -> str:
        return name.rsplit(":", 1)[-1]

    def start(name: str, attrs: dict[str, str]) -> None:
        at = p.CurrentByteIndex
        close = data.find(b">", at)
        node = Node(local(name), {local(k): v for k, v in attrs.items()}, at)
        node.inner = (close + 1, close + 1)
        if stack:
            stack[-1].children.append(node)
        else:
            top.append(node)
        stack.append(node)
        texts.append([])

    def end(name: str) -> None:
        at = p.CurrentByteIndex
        node = stack.pop()
        node.text = "".join(texts.pop())
        if data[at:at + 2] == b"</":
            node.inner = (node.inner[0], at)
            close = data.find(b">", at)
            node.end = close + 1
        else:  # an empty element: <a/>
            node.inner = (node.inner[0], node.inner[0])
            node.end = node.inner[0]

    def chars(text: str) -> None:
        if texts:
            texts[-1].append(text)

    def refuse(*_args: Any) -> None:
        raise ValueError("declarations are not read")

    p.StartElementHandler = start
    p.EndElementHandler = end
    p.CharacterDataHandler = chars
    p.StartDoctypeDeclHandler = refuse
    p.EntityDeclHandler = refuse
    try:
        p.Parse(data, True)
    except expat.ExpatError as e:
        raise ValueError(expat.ErrorString(e.code)) from None
    if not top:
        raise ValueError("no root element")
    return top[0]


def name_of(raw: str | None) -> str | None:
    """A name as written, with substitutions as `{name}`."""
    if not raw or not raw.strip():
        return None
    return re.sub(r"\$\{([^{}]*)\}", lambda m: placeholder(m.group(1)), raw.strip())


def property_names(node: Node | None) -> list[str]:
    """The names of a <parameters> or <configuration> block's properties (never their values)."""
    if node is None:
        return []
    return sorted({n for p in node.all("property") if (n := p.text_of("name"))})


# ------------------------------------------------------------------ shared


class _Definition:
    def __init__(self, out: Out, path: str, data: bytes, root: Node):
        self.out, self.path, self.data, self.root = out, path, data, root
        self.dir = posixpath.dirname(path)

    def at(self, node: Node):
        return self.out.span(node.start, node.end or node.start)

    def span(self, node: Node) -> list[int]:
        s = self.at(node)
        return [s.start_byte, s.end_byte]

    def job(self, term: str, kind: str, attrs: dict[str, Any]) -> dict[str, Any]:
        key = {"framework": FRAMEWORK, "name": self.path}
        return self.out.term(self.out.whole(), term, key, self.path, {"kind": kind, "app_name": name_of(self.root.attrs.get("name")), **attrs})

    def app_path(self, raw: str | None, default_file: str) -> str | None:
        """The repository path of an application a path names, when it is literal and relative."""
        if not raw:
            return None
        text = _APP_PATH.sub("", raw.strip())
        resolved = resolve_relative(self.dir, text)
        if resolved is None:
            return None
        return resolved if resolved.endswith(".xml") else posixpath.join(resolved, default_file)

    def unstated(self, node: Node, what: str) -> None:
        self.out.note("info", f"{what} named at run time in <{node.tag}> is not resolved")
        self.out.unresolved += 1


# ------------------------------------------------------------------ workflows


class _Workflow(_Definition):
    def run(self) -> None:
        out, root = self.out, self.root
        params = root.child("parameters")
        self.me = self.job("hdp:OozieWorkflow", "workflow", {"parameters": property_names(params)})
        out.construct(out.whole(), "hdp:oozie_workflow", True)
        if params is not None:
            out.construct(self.at(params), "hdp:oozie_parameters", True)
        for tag, cid in (("global", "hdp:oozie_global"), ("credentials", "hdp:oozie_credentials")):
            for n in root.all(tag):
                out.construct(self.at(n), cid, False)
        nodes = {n.attrs.get("name"): n for n in root.children if n.tag in ("action", *_CONTROL[1:]) and n.attrs.get("name")}
        self.nodes = nodes
        self.tasks: dict[str, dict[str, Any]] = {}
        for n in root.children:
            if n.tag == "action" and n.attrs.get("name"):
                self.action(n)
        for n in root.children:
            if n.tag in _CONTROL[1:]:
                cid = {"decision": "hdp:oozie_decision", "fork": "hdp:oozie_fork", "join": "hdp:oozie_join", "kill": "hdp:oozie_kill", "end": "hdp:oozie_end"}[n.tag]
                out.construct(self.at(n), cid, True)
        self.transitions()
        self.flow()

    # -------------------------------------------------- actions

    def action(self, node: Node) -> None:
        out = self.out
        name = node.attrs["name"]
        body = next((c for c in node.children if c.tag not in ("ok", "error")), None)
        kind = body.tag if body is not None else "unknown"
        cid = ACTION_CONSTRUCTS.get(kind, "hdp:oozie_action_other")
        out.construct(self.at(node), cid, cid != "hdp:oozie_action_other")
        attrs: dict[str, Any] = {"type": kind}
        cmd = None
        if body is not None:
            attrs.update(self.details(kind, body))
            if kind == "sqoop":
                cmd = self.sqoop_command(body)
                if cmd is not None:
                    from repo_intel.platforms.hadoop.sqoop import incremental_attrs

                    attrs.update({"tool": cmd.tool, **incremental_attrs(cmd)})
            for prep in body.all("prepare"):
                out.construct(self.at(prep), "hdp:oozie_prepare", False)
            for conf in body.all("configuration"):
                names = property_names(conf)
                if names:
                    attrs["configuration"] = names
        for sla in node.all("info"):  # <sla:info>, its prefix dropped
            out.construct(self.at(sla), "hdp:oozie_sla", False)
        key = {"framework": FRAMEWORK, "job": self.path, "name": name}
        ref = out.term(self.at(node), "hdp:OozieAction", key, name, attrs)
        out.relation(self.at(node), self.me, P.CONTAINS, ref)
        self.tasks[name] = ref
        if body is None:
            return
        if kind in ("hive", "hive2", "pig"):
            self.script(body, ref, "pig" if kind == "pig" else "hive")
        elif kind == "sqoop" and cmd is not None:
            self.sqoop(body, ref, cmd)
        elif kind == "shell":
            self.shell(body, ref)
        elif kind == "spark":
            self.spark(body, ref)
        elif kind == "distcp":
            self.distcp(body, ref)
        elif kind == "fs":
            self.fs(body, ref)
        elif kind == "sub-workflow":
            target = self.app_path(body.text_of("app-path"), "workflow.xml")
            if target is None:
                self.unstated(body, "the sub-workflow")
            else:
                out.relation(self.at(body), ref, P.RUNS, out.base.ref(K.JOB, {"framework": FRAMEWORK, "name": target}, target, declare=False))

    def details(self, kind: str, body: Node) -> dict[str, Any]:
        """Attributes that name things: scripts, classes, parameter names (never values)."""
        attrs: dict[str, Any] = {}
        script = body.text_of("script")
        if script:
            attrs["script"] = name_of(script)
        params = sorted({p.text.split("=", 1)[0].strip() for p in body.all("param") if "=" in p.text})
        if params:
            attrs["parameters"] = params
        for tag, attr in (("main-class", "main_class"), ("class", "main_class"), ("exec", "exec"), ("name", "app_name")):
            value = body.text_of(tag)
            if value and kind in ("java", "map-reduce", "spark", "shell"):
                attrs[attr] = name_of(value)
        if kind == "fs":
            attrs["operations"] = sorted({c.tag for c in body.children if c.tag not in ("name-node", "configuration", "job-xml")})
        return attrs

    def script(self, body: Node, task: dict[str, Any], platform: str) -> None:
        node = body.child("script")
        if node is None:
            if body.child("query") is not None:
                self.out.note("info", "an inline <query> of a hive action is not read")
            return
        target = resolve_relative(self.dir, _APP_PATH.sub("", node.text.strip()))
        if target is None:
            self.unstated(node, "the script")
            return
        ref = program(platform, target)
        self.out.relation(self.at(node), task, P.RUNS, self.out.base.ref(ref["kind"], ref["natural_key"], ref["name"], declare=False))
        self.out.resolved += 1

    def sqoop_command(self, body: Node):
        from repo_intel.extractors.sql_lineage import byte_offsets
        from repo_intel.platforms.hadoop.sqoop import Token, parse, split_command

        command = body.child("command")
        tokens: list[Token] = []
        if command is not None:
            raw = self.data[command.inner[0]:command.inner[1]]
            text = raw.decode("utf-8", "replace")
            to_byte = byte_offsets(text, raw)
            tokens = [Token(_unescape(t.text), command.inner[0] + t.start, command.inner[0] + t.end) for t in split_command(text, 0, to_byte)]
        else:
            for a in body.all("arg"):
                tokens.append(Token(a.text.strip(), a.inner[0], a.inner[1]))
        tokens = [t for t in tokens if t.text]
        if not tokens:
            return None
        return parse(tokens)

    def sqoop(self, body: Node, task: dict[str, Any], cmd) -> None:
        from repo_intel.platforms.hadoop.sqoop import constructs_of_incremental, emit

        out = self.out
        constructs_of_incremental(out, cmd)
        transforms = emit(out, cmd, task, self.at(body))
        out.transforms(self.at(body), task, transforms)

    def shell(self, body: Node, task: dict[str, Any]) -> None:
        exec_ = body.text_of("exec")
        if not exec_:
            return
        files = [f.text.strip() for f in body.all("file") if f.text.strip()]
        shipped = [f for f in files if posixpath.basename(f.split("#", 1)[0]) == posixpath.basename(exec_) or ("#" in f and f.split("#", 1)[1] == exec_)]
        if shipped:
            raw = shipped[0].split("#", 1)[0]
        elif "/" in exec_ or posixpath.splitext(exec_)[1] in (".sh", ".bash", ".ksh", ".py"):
            raw = exec_
        else:
            return  # a command on the node (`hdfs`, `echo`), not a file of the application
        target = resolve_relative(self.dir, _APP_PATH.sub("", raw))
        if target is None:
            self.unstated(body, "the shell script")
            return
        at = self.at(body)
        self.out.relation(at, task, P.RUNS, self.out.base.ref(K.ARTIFACT, {"path": target}, posixpath.basename(target), declare=False))
        if posixpath.splitext(target)[1].lower() in SHELL_SUFFIXES:
            # The shell pack declares the script as Program {shell, path}; the reference links
            # only when the repository has that script.
            ref = program("shell", target)
            self.out.relation(at, task, P.RUNS, self.out.base.ref(ref["kind"], ref["natural_key"], ref["name"], declare=False))
        self.out.resolved += 1

    def spark(self, body: Node, task: dict[str, Any]) -> None:
        jar = body.text_of("jar")
        if not jar:
            return
        target = resolve_relative(self.dir, _APP_PATH.sub("", jar))
        if target is None:
            self.unstated(body, "the spark application")
            return
        self.out.relation(self.at(body), task, P.RUNS, self.out.base.ref(K.ARTIFACT, {"path": target}, posixpath.basename(target), declare=False))
        self.out.resolved += 1

    def distcp(self, body: Node, task: dict[str, Any]) -> None:
        from repo_intel.compile.transforms import Builder

        out = self.out
        args = [a for a in body.all("arg") if a.text.strip() and not a.text.strip().startswith("-")]
        if len(args) < 2:
            self.unstated(body, "the copy")
            return
        b = Builder()
        found = [(a, dataset_path(a.text)) for a in args]
        *sources, target = found
        if target[1] is None:
            self.unstated(target[0], "the copy target")
            return
        for node, src in sources:
            if src is None:
                self.unstated(node, "the copy source")
                continue
            ref = out.dataset(self.at(node), src[0], src[1])
            out.moves(self.at(node), task, P.READS_FROM, ref, src[1], {"via": "distcp"})
            b.write(b.add("read", self.span(node), [src[0]]), target[1][0], self.span(target[0]))
        ref = out.dataset(self.at(target[0]), target[1][0], target[1][1])
        out.moves(self.at(target[0]), task, P.WRITES_TO, ref, target[1][1], {"via": "distcp"})
        from repo_intel.platforms.hadoop.common import mark_dynamic

        out.transforms(self.at(body), task, mark_dynamic(b.finish()))

    def fs(self, body: Node, task: dict[str, Any]) -> None:
        out = self.out
        for c in body.all("move"):
            src, dst = dataset_path(c.attrs.get("source", "")), dataset_path(c.attrs.get("target", ""))
            if src is None or dst is None:
                self.unstated(c, "the move")
                continue
            out.moves(self.at(c), task, P.READS_FROM, out.dataset(self.at(c), src[0], src[1]), src[1], {"via": "fs"})
            out.moves(self.at(c), task, P.WRITES_TO, out.dataset(self.at(c), dst[0], dst[1]), dst[1], {"via": "fs"})

    # -------------------------------------------------- order

    def targets(self, node: Node) -> list[tuple[Node, str, str]]:
        """(element, on, target name) of each transition out of a node."""
        if node.tag == "action":
            return [(c, c.tag, c.attrs["to"]) for c in node.children if c.tag in ("ok", "error") and c.attrs.get("to")]
        if node.tag == "decision":
            switch = node.child("switch")
            if switch is None:
                return []
            return [(c, "ok", c.attrs["to"]) for c in switch.children if c.tag in ("case", "default") and c.attrs.get("to")]
        if node.tag == "fork":
            return [(c, "ok", c.attrs["start"]) for c in node.all("path") if c.attrs.get("start")]
        if node.tag == "join":
            return [(node, "ok", node.attrs["to"])] if node.attrs.get("to") else []
        return []

    def reach(self, name: str, via: tuple[str, ...], seen: set[str]) -> list[tuple[str, tuple[str, ...]]]:
        """The actions a transition to `name` leads to, through control nodes, with the kinds passed."""
        node = self.nodes.get(name)
        if node is None or name in seen:
            return []
        if node.tag == "action":
            return [(name, via)]
        seen = seen | {name}
        out = []
        for _, _, to in self.targets(node):
            out += self.reach(to, via + (node.tag,) if node.tag not in via else via, seen)
        return out

    def transitions(self) -> None:
        out = self.out
        found: dict[tuple[str, str, str], dict[str, Any]] = {}
        for node in self.root.children:
            if node.tag == "start":
                continue
            for el, on, to in self.targets(node):
                if node.tag == "action":
                    known = to in self.nodes
                    out.construct(self.at(el), "hdp:oozie_transition", known)
                    if not known:
                        out.note("warning", f"transition to {to!r}, which is not a node of this workflow")
                        continue
                    for down, via in self.reach(to, (), set()):
                        key = (down, node.attrs["name"], on)
                        if key not in found:
                            found[key] = {"at": self.at(el), "via": via}
        for (down, up, on), info in found.items():
            if down == up:
                continue  # a retry loop back to itself
            q: dict[str, Any] = {"on": on}
            if info["via"]:
                q["via"] = "+".join(info["via"])
            out.relation(info["at"], self.tasks[down], P.DEPENDS_ON, self.tasks[up], q)

    def flow(self) -> None:
        """The behaviour unit of the workflow, in document order."""
        body: list[dict[str, Any]] = []
        for node in self.root.children:
            stmt = self.statement(node)
            if stmt is not None:
                body.append(stmt)
        if body:
            self.out.behavior(self.out.whole(), self.me, body, kind="flow")

    def go(self, el: Node, on: str, to: str) -> dict[str, Any]:
        return {"op": "platform", "construct": "hdp:oozie_transition", "at": self.span(el), "on": on, "to": to}

    def statement(self, node: Node) -> dict[str, Any] | None:
        at = self.span(node)
        name = node.attrs.get("name", "")
        if node.tag == "start" and node.attrs.get("to"):
            return {"op": "platform", "construct": "hdp:oozie_transition", "at": at, "on": "start", "to": node.attrs["to"]}
        if node.tag == "action" and name:
            ok = [self.go(el, on, to) for el, on, to in self.targets(node) if on == "ok"]
            err = [self.go(el, on, to) for el, on, to in self.targets(node) if on == "error"]
            call = {"op": "call", "at": at, "call": {"e": "call", "callee": {"e": "name", "name": name, "kind": "procedure"}, "args": []}}
            stmt: dict[str, Any] = {"op": "try", "at": at, "body": [call, *ok]}
            if err:
                stmt["catches"] = [{"type": "error", "body": err}]
            return stmt
        if node.tag == "decision":
            switch = node.child("switch")
            if switch is None:
                return None
            cases = [c for c in switch.all("case") if c.attrs.get("to")]
            default = switch.child("default")
            tail: list[dict[str, Any]] = [self.go(default, "default", default.attrs["to"])] if default is not None and default.attrs.get("to") else []
            for c in reversed(cases):
                cond = {"e": "platform", "construct": "hdp:oozie_decision", "text": " ".join(c.text.split())}
                stmt = {"op": "if", "at": self.span(c), "cond": cond, "then": [self.go(c, "case", c.attrs["to"])]}
                if tail:
                    stmt["else"] = tail
                tail = [stmt]
            if not tail:
                return None
            tail[0]["at"] = at
            return tail[0]
        if node.tag == "fork":
            return {"op": "platform", "construct": "hdp:oozie_fork", "at": at, "name": name, "paths": [p.attrs["start"] for p in node.all("path") if p.attrs.get("start")]}
        if node.tag == "join":
            return {"op": "platform", "construct": "hdp:oozie_join", "at": at, "name": name, "to": node.attrs.get("to", "")}
        if node.tag == "kill":
            return {"op": "throw", "at": at, "value": {"e": "lit", "value": name, "type": "string"}}
        if node.tag == "end":
            return {"op": "exit", "at": at}
        return None


def _unescape(text: str) -> str:
    from xml.sax.saxutils import unescape

    return unescape(text, {"&quot;": '"', "&apos;": "'"})


# ------------------------------------------------------------------ coordinators and bundles


def frequency(raw: str | None) -> dict[str, Any]:
    """The schedule a coordinator states: `{every, unit}` for an EL frequency or minutes, the
    text for a cron expression; nothing for a frequency set by a parameter."""
    if not raw:
        return {}
    text = raw.strip()
    m = _FREQ.match(text)
    if m:
        return {"schedule": text[2:-1].strip(), "frequency": {"every": int(m.group(2)), "unit": _UNITS[m.group(1)]}}
    if re.fullmatch(r"\d+", text):
        return {"schedule": text, "frequency": {"every": int(text), "unit": "minute"}}
    if "${" not in text and re.fullmatch(r"[\d*/,?LW#A-Z-]+(?:\s+[\d*/,?LW#A-Z-]+){4,5}", text, re.I):
        return {"schedule": text}
    return {}


class _Coordinator(_Definition):
    def run(self) -> None:
        out, root = self.out, self.root
        params = root.child("parameters")
        tz = root.attrs.get("timezone")
        attrs = {**frequency(root.attrs.get("frequency")), "timezone": tz if tz and "${" not in tz else None, "parameters": property_names(params)}
        if not frequency(root.attrs.get("frequency")):
            out.note("info", "the coordinator's frequency is set at run time")
        self.me = self.job("hdp:OozieCoordinator", "coordinator", attrs)
        out.construct(out.whole(), "hdp:oozie_coordinator", True)
        if params is not None:
            out.construct(self.at(params), "hdp:oozie_parameters", True)
        datasets: dict[str, dict[str, Any]] = {}
        block = root.child("datasets")
        if block is not None:
            for inc in block.all("include"):
                out.construct(self.at(inc), "hdp:oozie_coord_dataset", False)
                out.note("info", "datasets included from another file are not read here")
            for d in block.all("dataset"):
                uri = d.child("uri-template")
                found = dataset_path(uri.text) if uri is not None else None
                out.construct(self.at(d), "hdp:oozie_coord_dataset", found is not None)
                if found is None or not d.attrs.get("name"):
                    if d.attrs.get("name"):
                        self.unstated(d, "the dataset path")
                    continue
                ref = out.dataset(self.at(d), found[0], found[1])
                datasets[d.attrs["name"]] = {"ref": ref, "param": found[1]}
        for tag, pred, cid in (("input-events", P.READS_FROM, "hdp:oozie_coord_input"), ("output-events", P.WRITES_TO, "hdp:oozie_coord_output")):
            events = root.child(tag)
            for ev in events.children if events is not None else []:
                if ev.tag not in ("data-in", "data-out"):
                    continue
                d = datasets.get(ev.attrs.get("dataset", ""))
                out.construct(self.at(ev), cid, d is not None)
                if d is None:
                    self.unstated(ev, "the event's dataset")
                    continue
                out.moves(self.at(ev), self.me, pred, d["ref"], d["param"], {"event": ev.attrs.get("name")})
        action = root.child("action")
        wf = action.child("workflow") if action is not None else None
        if wf is None:
            return
        raw = wf.text_of("app-path")
        target = self.app_path(raw, "workflow.xml")
        at = self.at(wf.child("app-path") or wf)
        if target is not None:
            out.construct(at, "hdp:oozie_coord_action", True)
            out.relation(at, self.me, P.RUNS, out.base.ref(K.JOB, {"framework": FRAMEWORK, "name": target}, target, declare=False))
            out.resolved += 1
            return
        last = posixpath.basename((raw or "").rstrip("/"))
        if last == "workflow.xml":
            last = posixpath.basename(posixpath.dirname((raw or "").rstrip("/")))
        if self.dir and last == posixpath.basename(self.dir):
            # The usual layout: the coordinator sits in the workflow's application directory.
            target = posixpath.join(self.dir, "workflow.xml")
            out.construct(at, "hdp:oozie_coord_action", True)
            out.relation(at, self.me, P.RUNS, out.base.ref(K.JOB, {"framework": FRAMEWORK, "name": target}, target, declare=False), {"resolved": "same_directory"}, EvidenceClass.E4)
            out.resolved += 1
            prefix = re.match(r"\s*\$\{\s*([^{}]*?)\s*\}", raw or "")
            what = f"starts with ${{{prefix.group(1)}}}, which is set at run time" if prefix else "is not a path in the repository"
            out.note("info", f"the coordinator's workflow app-path {what}: RUNS is resolved to {target}, the workflow in the same directory, at E4")
            return
        out.construct(at, "hdp:oozie_coord_action", False)
        self.unstated(wf, "the workflow")


class _Bundle(_Definition):
    def run(self) -> None:
        out, root = self.out, self.root
        params = root.child("parameters")
        self.me = self.job("hdp:OozieBundle", "bundle", {"parameters": property_names(params)})
        out.construct(out.whole(), "hdp:oozie_bundle", True)
        if params is not None:
            out.construct(self.at(params), "hdp:oozie_parameters", True)
        for c in root.all("coordinator"):
            target = self.app_path(c.text_of("app-path"), "coordinator.xml")
            out.construct(self.at(c), "hdp:oozie_bundle_coordinator", target is not None)
            if target is None:
                self.unstated(c, "the coordinator")
                continue
            out.relation(self.at(c), self.me, P.RUNS, out.base.ref(K.JOB, {"framework": FRAMEWORK, "name": target}, target, declare=False))
            out.resolved += 1
