# Solution IR contract

Status: 2026-11-25, amended after the wave-1 review. This is the contract every workstream building the solution IR follows, so that their work fits together without negotiation. It implements [Toward a complete solution IR](vscode-plugin-plan.md#toward-a-complete-solution-ir). The shared foundation it describes is built and tested (`tests/test_platforms.py`); everything else is built against it.

If something here is wrong or missing for your workstream, do not change a shared file. Write the request under **Requests for the foundation** in your report, with the reason, and work around it in your own files.

## 1. Layers

Every entity has one **core kind** from `repo_intel/compile/ontology.py`. Consumers, views and code generators read core kinds only. Platform detail lives in attributes, named by a **platform term**.

### Core kinds added for the solution IR

| Kind | Meaning | Natural key |
|---|---|---|
| `Screen` | A page, view or report screen a user reaches | `{platform, name}`; `name` is the route, or the template path from the repository root |
| `Form` | An input form on a screen | `{screen, name}`; `screen` is the screen's `name`, `name` the form's name or `form<n>` in document order |
| `UIControl` | An input, select, button, link or display element | `{container, name}`; `container` is `<screen>#<form>` or `<screen>`; `name`: see UIControl names below |
| `StyleToken` | A design value: colour, type size, spacing, radius | `{system, name}`; `system` is the stylesheet path from the repository root or the design-system name, `name` the variable or class |
| `Report` | An output a person reads: ODS, PDF, spreadsheet | `{platform, name}` |
| `BusinessRule` | A validation, calculation or decision | `{scope, name}`; `scope` is the owning entity's name |
| `Workflow` | States and transitions of a business object | `{scope, name}` |
| `CodeList` | Reference data: a lookup table, SAS format, enum | `{platform, name}` |
| `Role`, `Permission` | Security principals and rights | `{realm, name}`; `realm` is the platform or application (`cfml`, `sas-metadata`, the database name) |

**UIControl names.** Both packs that read markup name a control by the first of these that applies, after trimming whitespace (an empty value does not count):

1. its `name` attribute, else its `id`, else its binding path (`v-model`, `formControlName`);
2. `link:<target>` for a link whose target is literal, with the target's query string and fragment removed (a link to another host keeps only `<scheme>://<host>`, where host includes a port, as in a URL's origin);
3. `submit` for a button whose `type` is `submit` (a button with no `type` is not counted as one, so a name never depends on the browser default), `button:<text>` for another button, where text is the button's value or all its descendant text, at most 40 characters, nothing computed;
4. otherwise `<type><n>`: `type` is an input's `type` attribute (`text` when absent), else the tag (`select`, `textarea`, `button`, and `link` for `<a>`); `n` counts, in document order within the container, only the controls that reach this rule, so naming or adding another control renames nothing.

When two controls in one container would get the same name, the second and later ones get `~2`, `~3`, ... in document order.

Existing kinds keep their keys. Legacy code units stay `Program` (`{platform, name}`), functions and methods stay `Symbol`.

### Relations added

| Predicate | Subject → object | Example |
|---|---|---|
| `RENDERS` | code (`Program`, `Symbol`, `Component`, `Package`, `Notebook`) → `Screen`, `Report` | `orders.cfm` RENDERS screen `orders` |
| `SUBMITS_TO` | `Form` → `Endpoint`, `Program`, `Symbol`, `Screen` | the order form SUBMITS_TO `orders_save.cfm` |
| `NAVIGATES_TO` | `Screen`, `Form`, `UIControl`, `Program`, `Symbol` → `Screen`, `Endpoint`, `Program` | a link, or a server-side redirect (`cflocation`) from code |
| `BINDS_TO` | `UIControl`, `Form` → `Field`, `CodeList`, `ConfigKey` | a select BINDS_TO the `order_status` code list |
| `STYLED_BY` | `Screen`, `Form`, `UIControl`, `Report` → `StyleToken` | |
| `ENFORCES` | code, UI, jobs, `Table`, `Field` → `BusinessRule` | |
| `APPLIES_TO` | `BusinessRule`, `Workflow` → data, UI, `Field`, `Endpoint`, `Task`, `Program` | |
| `REQUIRES_ROLE` | code, UI, `Endpoint`, `Report`, `Dataset`, `Table`, `Job` → `Role` | `IsUserInRole("admin")` guarding a page |
| `GRANTS` | `Role` → `Permission`, `Role` | |
| `CONTAINS` | now also from `Program`, `Screen`, `Form`, `Workflow` | screen CONTAINS form CONTAINS control; a component Program CONTAINS its function Symbols |
| `READS_FROM` | objects now include `CodeList` | a DATA step applying a format |
| `PRODUCES` | objects now include `Report` | `PROC REPORT` with ODS |

`check_relation` enforces these. The publish gate refuses any other subject or object kind.

### Platform terms and fidelity

A pack names its own constructs as terms, `<prefix>:<Name>` (`cf:Template`, `sas:Format`, `web:Route`), each mapped to one core kind with a fidelity:

- `exact`: the core kind says everything the construct means.
- `lossy`: the core kind is right; native semantics live only in attributes.
- `unmapped`: no core kind fits yet; the entity uses the nearest one and says so.

Declare platform entities with `repo_intel.platforms.platform_entity(blob, at, term, natural_key, name, attributes)`. It sets `attributes.platform` (the pack id), `attributes.platform_kind` (the term) and, unless exact, `attributes.fidelity`. The publish gate refuses an entity whose `platform_kind` is unregistered or maps to another kind. Neutral things a pack finds (a `Table` named in SQL) are declared with the ordinary `entity()` and no platform attributes.

## 2. Platform packs

A pack is a subpackage `repo_intel/platforms/<id>/` whose `__init__.py` defines `PACK = PlatformPack(...)`:

```python
PACK = PlatformPack(
    id="cfml", name="ColdFusion (CFML)", prefix="cf", version="1.0.0",
    terms=(Term("cf:Template", EntityKind.SCREEN, "lossy", "a .cfm page"), ...),
    constructs=(Construct("cf:cflock", "named or scope lock", "behavior:lock"), ...),
    extractors=(CfmlSource(),),
    supersedes=("legacy.cfml",),
    inputs=(".rkp/inputs/cfml/**",),
)
```

Rules:

1. **Imports.** A pack may import `repo_intel.compile.*` and `repo_intel.platforms` at module level. It imports `repo_intel.extractors.base` and other `repo_intel.extractors.<module>` modules only inside functions, and never the `repo_intel.extractors` package root: pack discovery runs while that package loads, and refuses a pack that is still importing.
2. **Extractor ids** are `<pack id>.<name>`. An extractor sees one file's bytes. It never runs, imports or installs repository code, never reads another file, and never copies configuration values, only names (values may be secrets).
3. **Superseding.** A pack that replaces a legacy extractor lists it in `supersedes`, and must produce at least everything that extractor did: the existing goldens (F14 for SAS, F15 for ColdFusion) are the proof and must keep passing unchanged.
4. **Versioning.** Each extractor's version folds in its code digest and its pack's terms and constructs. Raise `version` on any intended output change.
5. **Evidence.** Every entity, relation, construct and behaviour node has a span into the file. Classes: `E1` declared (a manifest or declarative config), `E2` parsed, `E3` resolved across files, `E4` heuristic. Extractors never emit `E5`; models do that through the decision runner.
6. **No guessing.** A dynamic construct whose target cannot be read from the source (a variable `cfinclude`, a macro variable resolved at run time) is modelled as dynamic (a non-literal expression) and reported with a diagnostic, never resolved by guesswork.
7. **URLs and other values.** Structural output and behaviour literals keep a URL's scheme, host, path and query parameter *names*, never query values, fragments or user information: `login.cfm?token=abc123` is written `login.cfm?token=`. Mail addresses, passwords, keys and connection strings are never copied. SQL text in a `query` node is kept as written; URL literals inside it are not rewritten yet.
8. **Size.** Extraction is linear in the file's size. Files over the snapshot's parse limit are not parsed; do not work around it.

### Construct coverage

Every construct the pack lists is reported on each occurrence with `repo_intel.platforms.construct(blob, at, construct_id, modelled)` (pass `evidence_class=EvidenceClass.E1` for constructs read from a declarative export). `modelled=True` only if the occurrence became an entity, relation, attribute or behaviour node. The compiler records the counts on the file's `Artifact` (`attributes.constructs`) and writes one `platform_constructs` coverage entry per platform: occurrences modelled over occurrences seen, per-construct counts, and the constructs looked for but not seen. List the constructs a real application uses, not only the ones you model: an unmodelled construct counted is the point.

### Inputs from outside the repository

Exports of platform configuration (ColdFusion Administrator `neo-*.xml`, SAS `autoexec.sas` and `config.sas`, scheduler definitions, database catalogue dumps) are committed by the owner under `.rkp/inputs/<pack id>/` and read by the pack's extractors like any other file. Record the pattern in `PACK.inputs`. Catalogue dumps that are not platform-specific go under `.rkp/inputs/catalog/` and belong to the solution workstream.

## 3. The behavioural IR

`repo_intel/compile/behavior.py` defines it; `validate_unit` is the contract. A pack emits one `behavior` observation per function, page, DATA step, macro or procedure with `behavior_observation(blob, at, owner, language, body, params)`, where `owner` is the `ref(...)` of the `Symbol` or `Program` it belongs to. The function validates before emitting; invalid output raises.

A unit may carry `kind` (`function`, `page`, `macro`, `data_step`, `sql_step`, `proc_step`, `open_code`, `handler`; pass `kind=` to `behavior_observation`) to tell apart several units of one owner, such as a SAS program's DATA steps.

Statements (`op`): `assign`, `declare`, `call`, `if`, `switch`, `loop` (`kind`: `for`, `while`, `until`, `each`, `range`, `implicit` — a DATA step's row loop is `implicit`), `break`, `continue`, `return`, `throw`, `try`, `query` (SQL text with `:name` parameters, `params`, `datasource`, optional `target`; a parameter with `"inline": true` is text substituted into the SQL before it runs, CFML `#x#` or SAS `&x`, not a bind parameter, and consumers treat it as dynamic text; a query whose text, or part of it, runs in another database (SAS pass-through) carries `"remote": "<engine>"`, and consumers never read table names from that part), `output` (`channel`: `response`, `log`, `file`, `report`, `dataset`, `mail`), `include`, `redirect`, `transaction`, `lock`, `exit`, and `platform` for anything else.

Expressions (`e`): `lit` (with `type`), `var` (with `scope`: `form`, `url`, `session`, `application`, `request`, `cgi`, `local`, `arguments`, `variables`, `this`, `macro`, `work`, ...), `member`, `index`, `call`, `name`, `binop`, `unop`, `cond`, `new`, `array`, `struct`, `lambda`, `template` (interpolation), and `platform`.

Each node may carry `"at": [start_byte, end_byte]`; give it on every statement. A construct with no neutral form is `{"op": "platform", "construct": "sas:retain", "detail": {...}}`, named by a construct id the pack lists. Example:

```json
{"op": "if", "at": [120, 410],
 "cond": {"e": "binop", "op": ">", "left": {"e": "var", "scope": "form", "name": "qty"}, "right": {"e": "lit", "value": 0, "type": "number"}},
 "then": [{"op": "query", "at": [160, 380], "sql": "UPDATE orders SET qty = :qty WHERE id = :id",
           "params": [{"name": "qty", "value": {"e": "var", "scope": "form", "name": "qty"}, "type": "integer"}],
           "datasource": "shopdsn"}]}
```

The compiler stores each unit as a row of its shard's `behaviors` table, keyed by the owner's entity id (the table is present only when it has rows). Behaviour is function-level detail: the bundle keeps it only in the full tier (`bundle.DETAIL_TABLES`), never in the committed architecture tier. A unit whose owner is not declared in the same unit, or that no longer validates, becomes an Unknown (predicate `behavior_owner`, coverage ref `behavior:<unit>`) counted in the unit's `behavior` coverage. Read it through `repo_intel.knowledge.behavior_store` or the local API, `GET snapshots/{h}/behavior/{entity_id}`.

When two declarations of one entity disagree on platform (a JCL and a SAS `Dataset`), the compiler keeps the platform attributes of the one declared through a term and lists both platforms in `attributes.platforms`.

## 4. Workstreams (wave 1)

Each workstream works on its own branch from `claude/solution-ir-foundation`, named below, and touches only the files listed. Everything else is read-only for it.

| | Workstream | Branch | May create or change |
|---|---|---|---|
| W1 | ColdFusion pack: a real CFML parser (tags and script), behaviour for every function and page, `Application.cfc` lifecycle and scopes, screens, forms and controls from templates, roles from `cflogin`/`IsUserInRole`/`roles=`, datasources and scheduled tasks from `.rkp/inputs/cfml/` | `claude/ir-cfml` | `repo_intel/platforms/cfml/**`, `tests/test_platform_cfml.py`, `benchmarks/fixtures/F20-coldfusion-app/**` |
| W2 | SAS pack: a parser with symbolic macro expansion, DATA-step and PROC lowering, `PROC FORMAT` as `CodeList`, ODS and `PROC REPORT` as `Report`, stored-process prompts as `Form`s, `LIBNAME` and `autoexec`/`config.sas` from `.rkp/inputs/sas/` | `claude/ir-sas` | `repo_intel/platforms/sas/**`, `tests/test_platform_sas.py`, `benchmarks/fixtures/F21-sas-analytics/**` |
| W3 | Web experience pack: screens, forms, controls and navigation from HTML, JSX/TSX, Vue and Angular templates and router configuration; `StyleToken`s from CSS, SCSS and LESS and which screens use them | `claude/ir-web` | `repo_intel/platforms/web/**`, `tests/test_platform_web.py`, `benchmarks/fixtures/F22-web-frontend/**` |
| W4 | Behaviour store: compile `behavior` observations into shards, keep them in the bundle's detail tier, answer them in the local API (`GET snapshots/{h}/behavior/{entity_id}`), derive call edges and data access from them | `claude/ir-behavior` | `repo_intel/compile/compiler.py` (behaviour only), `repo_intel/knowledge/bundle.py`, `repo_intel/knowledge/local_api.py`, `repo_intel/knowledge/behavior_store.py` (new), `tests/test_behavior_store.py`, `packages/bundle/**` (the TypeScript reader) |
| W5 | Solution manifest: `.rkp/solution.json` listing the repositories and inputs of one application, loading several bundles, resolving one entity across them (exact rules first; candidates at `E4`), and database catalogue dumps from `.rkp/inputs/catalog/` | `claude/ir-solution` | `repo_intel/solution/**` (new), `repo_intel/platforms/catalog/**`, `repo_intel/cli.py` (a `solution` command only), `tests/test_solution.py`, `tests/fixtures/solution/**` (the golden format is single-repository, so F23 is test-only) |
| W6 | Commands and views (also owns `repo_intel/knowledge/bundle_check.py`, the shared bundle reader, and the command documentation in `repo-intelligence/README.md`): `rkp verify` (re-check a bundle against its digests and the publish gate) and `rkp diff` (structural diff between two bundles, using `repo_intel/knowledge/delta.py`), and the Explorer's experience, business and security planes | `claude/ir-views` | `repo_intel/cli.py` (`verify`, `diff` only), `repo-intelligence/explorer/**`, `tests/test_cli_verify_diff.py` |

`repo_intel/cli.py` is shared by W5 and W6; each adds its own subcommand in a separate function and touches nothing else in the file. The integrator merges them.

Shared, read-only for every workstream: `repo_intel/compile/ontology.py`, `repo_intel/compile/behavior.py`, `repo_intel/compile/contracts.py`, `repo_intel/compile/verify.py`, `repo_intel/platforms/__init__.py`, `repo_intel/extractors/__init__.py`, `repo_intel/extractors/base.py`, every existing extractor, every existing fixture and golden, and this document.

### Solution identity (W5)

A link between members is exact (`E3`) only when the natural keys say the two are one thing: a table by `{datastore, name}`, an endpoint by method and normalised path including a `?method=` selector, a queue or topic by name, a SAS dataset by `{library, name}` when both sides are SAS. A ConfigKey matched by name alone is exact only when one side is authoritative: the solution's inputs, a member whose role is `config` or `database`, or a declaration read from an export under `.rkp/inputs/` (evidence `E1`). Every other name-only match, ConfigKeys of the same kind of source included, is a candidate at `E4`.

## 5. Tests and fixtures

- Each pack has `tests/test_platform_<id>.py`: extraction of realistic snippets, every emitted behaviour unit passing `validate_unit`, construct counts, natural keys, and the superset rule against the legacy extractor it replaces.
- Each pack adds one golden fixture (`F2x-…`, format in `benchmarks/fixtures/README.md`) whose `golden.json` lists the entities and relations a correct compile must produce, with `since: "M2"`.
- Before reporting done, a workstream runs:

  ```bash
  .venv/bin/python -m pytest -q -p no:warnings
  ```

  ```bash
  .venv/bin/python -m repo_intel.compile.golden_check
  ```

  Both must pass. A workstream never edits another fixture's golden to make it pass. Sandboxed compile workers load the engine from the checkout under test, so the shared virtual environment works from any worktree with `PYTHONPATH=.`.

## 6. Review checklist

The integrator checks every branch against this list before merging:

1. Only the files the workstream owns are changed (`git diff --stat claude/solution-ir-foundation...`).
2. Every platform entity is declared through a term; every term maps to the right core kind with an honest fidelity.
3. Every relation is permitted by the schema; the publish gate passes on the new fixture.
4. Every construct of the platform a real application uses is listed and counted, modelled or not.
5. Behaviour units validate, carry spans, and use `platform` nodes rather than inventing ops.
6. No guessing: dynamic targets stay dynamic, with a diagnostic.
7. No configuration values, no secrets, no absolute paths in output.
8. Superseded extractors' goldens still pass unchanged.
9. The full test suite and `golden_check` pass.
10. **Requests for the foundation** are listed, with reasons.

## 7. Reporting

Each workstream ends with a short report: what it built, what it did not, test results with counts, coverage on its fixture (`platform_constructs` numerator and denominator), and Requests for the foundation. Commits are made on the workstream's branch, ending with the attribution line used in this repository. Workstreams do not push; the integrator merges and pushes.
