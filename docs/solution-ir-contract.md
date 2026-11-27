# Solution IR contract

Status: 2026-11-26, amended after the wave-1 review, extended for wave 2 (section 8) and for wave W7 (sections 9 and 10). This is the contract every workstream building the solution IR follows, so that their work fits together without negotiation. It implements [Toward a complete solution IR](vscode-plugin-plan.md#toward-a-complete-solution-ir). The shared foundation it describes is built and tested (`tests/test_platforms.py`); everything else is built against it.

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
| `BINDS_TO` | `UIControl`, `Form`, `Field` (to a `CodeList` only, section 11.1) → `Field`, `CodeList`, `ConfigKey` | a select BINDS_TO the `order_status` code list |
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

`repo_intel/compile/behavior.py` defines it; `validate_unit` is the contract. A pack emits one `behavior` observation per function, page, DATA step, macro or procedure with `behavior_observation(blob, at, owner, language, body, params)`, where `owner` is the `ref(...)` of the `Symbol` or `Program` it belongs to, or, for unit kinds `flow` and `open_code`, of the `Job` or `Task` (a workflow's order, a job step's inline commands). The function validates before emitting; invalid output raises.

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

## 8. Wave 2

Wave 2 builds on the integrated wave 1 (`claude/solution-ir`). Sections 1 to 7 still apply; where this section names a different branch, base or file list, it replaces section 4 for wave 2. Each workstream branches from the commit that adds this section.

### 8.1 Transform shapes (W1)

A transform shape says what a unit does to the rows it reads. It is derived from what the engine already parses and never needs a model. The vocabulary comes from pipeline-builder (sourceform), which uses it to generate pipelines; here it is used to recognise them.

`repo_intel/compile/transforms.py` (new, owned by W1) defines schema `rkp-transform/1`, `validate_shape`, and `transform_observation(blob, at, owner, language, transforms)`, which validates and returns an observation of kind `transform`. One observation describes one unit: a stand-alone SQL procedure or script (all its statements, so the SCD2 pass can see an UPDATE and its INSERT together; each transform's `at` keeps the statement span), a DATA step, a PROC step, a behaviour unit such as a `cfquery`, or a PySpark chain.

```json
{"owner": {"kind": "Program", "natural_key": {...}, "name": "..."},
 "language": "sql" | "sas" | "pyspark",
 "transforms": [
   {"id": "t1", "op": "deduplicate", "at": [120, 410],
    "inputs": ["work.orders_sorted"], "output": "work.orders_latest",
    "keys": ["order_id"], "order": [{"column": "updated_at", "desc": true}],
    "primitive": "dedupe_by_key"}]}
```

Fields of a transform:

| Field | Meaning |
|---|---|
| `id` | `t1`, `t2`, ... in source order within the unit |
| `op` | one of `read`, `project`, `filter`, `join`, `union`, `deduplicate`, `aggregate`, `window`, `pivot`, `unpivot`, `sort`, `validate`, `publish`, `platform` |
| `at` | span of the clause or statement it comes from; required |
| `inputs` | dataset or table names as written (lower-cased, library or schema kept), or ids of earlier transforms in the unit |
| `output` | the name written, or absent when the result only feeds the next transform |
| `keys` | partition, group, join or BY columns |
| `order` | `[{"column", "desc"}]` |
| `join` | `{"type": inner\|left\|right\|full\|semi\|anti\|cross, "on": [[left, right]]}` |
| `columns` | the column effect, pipeline-builder's `Shape`: `{"add": [...], "replace": [...], "drop": [...], "rename": {old: new}}`; casts and derived columns are `replace` or `add` on a `project` |
| `predicate` | a behavioural-IR expression (section 3), or `{"sql": text}` when only SQL text is available |
| `measures` | `[{"fn", "column", "as"}]` for `aggregate` and `window` |
| `primitive` | a name from `PRIMITIVES`, only when the whole idiom matched exactly |
| `candidates` | primitive names a partial match suggests; never with `primitive` |
| `dynamic` | `true` when an input or output name is not literal (macro variable, f-string); the name keeps its `{placeholder}` |
| `construct` | for `op: platform`, the construct or function that was not recognised |

`PRIMITIVES` maps each recognisable primitive name to its pipeline-builder category. Start with these and add only names whose idiom can be matched exactly: `select_columns`, `drop_columns`, `rename_columns`, `safe_cast`, `derive_column`, `conditional_flag`, `map_codes`, `coalesce_columns`, `apply_defaults`, `trim_strings`, `normalize_case`, `parse_timestamp`, `split_by_condition`, `union_by_name`, `join`, `dedupe_by_key`, `dedupe_by_hash`, `aggregate`, `window_function`, `calculate_running_total`, `delta_calc`, `top_n_filter`, `pivot`, `unpivot`, `merge_into`, `apply_scd_type_2`, `apply_deletes`. Categories use pipeline-builder's names, snake-cased: `structural_reshaping`, `type_standardization`, `cleaning`, `deduplication`, `cdc`, `integration`, `business_rules`, `aggregation`. Names that only describe business meaning (`calculate_score`, `classify_record`, `derive_status`) are never emitted here; they are for rule lifting.

Where shapes come from:

- **Behaviour units** (CFML `cfquery`, SAS DATA steps, PROC SQL and other PROC steps): derived in the compiler from the stored behaviour, after step 4b, so no pack has to emit them. A `query` node that is `remote` or whose table names are dynamic gives `op: platform` with `dynamic: true`. The SAS pack may add detail to its own `platform` nodes (PROC options such as `nodupkey` and `out=`, KEEP, DROP and RENAME lists) so the derivation has what it needs.
- **Stand-alone SQL** (`sql_lineage.SqlScripts`) and **PySpark** (`data_python`): those extractors emit `transform` observations themselves, using the shared SQL recognizer in `transforms.py` (sqlglot trees, never run).

Storage: the compiler writes one row per observation to the shard's `transforms` table (`{"owner": <entity id>, "path", "at", "language", "transforms"}`), sorted, present only when it has rows. It is detail: `bundle.DETAIL_TABLES` and `_OPTIONAL_TABLES` list it. The owner entity gets `attributes.transforms = {"primitives": [...sorted names], "purpose": <category>}` in every tier; `purpose` is the category with the most primitives in the owner's units, ties broken by the order above, absent when there are none. `behavior_store` returns the owner's transforms beside its behaviour, and the local API's behaviour answer includes them. Coverage domain `transform_shapes`: units with at least one non-`platform` transform over units that move data; `detail.statements` gives `{recognised, total}` statement counts so a script with one recognised statement out of ten is visible.

### 8.2 Rules, workflows and captured screens from models (W2)

Model output follows the DM08 pattern (`repo_intel/decisions/behavior.py`): a decision spec, egress denied unless policy names the provider, abstain on any failure, evidence class `E5`, citations checked against the evidence supplied.

- **Business rules.** Candidates are deterministic: conditions and computed values in behaviour units (a CFML `if` whose branch throws, redirects or outputs an error; SAS `WHERE`, subsetting `IF` and assignments; SQL `WHERE` and `CASE`), each with its unit's transform shapes (8.1) as context, read from the shard's `transforms` rows. Spec `dm.business-rules` names and groups candidates into rules. A rule is `BusinessRule {scope: <owner entity name>, name: <lower-case slug from the model, at most 60 characters>}` with attributes `statement` (at most 280 characters), `rule_kind` (`validation`, `calculation`, `decision`), `status` (`hypothesis`, or `confirmed` after an owner override), and the candidate spans it cites. Relations: owner `ENFORCES` rule; rule `APPLIES_TO` the `Field`, `Table`, `Dataset` or `Form` the condition reads, when that entity already exists. Assertions are `hypothesis`, `inferred`, `E5`.
- **Workflows.** States come from literal assignments to a column named `status`, `state` or `stage` (or a column a CodeList constrains) across units; transitions from the conditions guarding them. The states and transitions found are facts (`E2`/`E3`); spec `dm.workflows` only names the workflow and its states. `Workflow {scope: <table or dataset name>, name: <column>}`, attributes `states`, `transitions` (each with evidence), `status`. `Workflow APPLIES_TO` the Table or Dataset.
- **Confirmation.** An owner confirms or rejects with a `decisions` override: `spec` is `dm.business-rules` or `dm.workflows`, `scope_entity` the rule or workflow's entity id, `label` `confirmed` or `rejected`. Confirmed gives `status: confirmed` and epistemic status `supported`; rejected drops it from the snapshot and keeps a conflict record. Nothing is confirmed by default.
- **Captured screens (E5).** Owner-captured evidence under `.rkp/inputs/screens/`: a `capture.json` listing `{route or template, image, dom}` per capture, PNG images and saved DOM HTML. A deterministic step reads `capture.json` and the DOM (regions by landmark and heading, controls by the naming rule of section 1); spec `dm.screen-regions` labels regions. Results attach to the existing `Screen` (matched by route or template) as `attributes.captured` with `E5` evidence pointing at the capture; an unmatched capture is a diagnostic. No browser runs inside the engine.
- Rules and workflows belong in the committed architecture tier; captured regions are detail.

### 8.3 HTTP calls in the committed bundle (W3)

- `typescript_source` records the HTTP method of a client call (`fetch` `method:` literal, `axios.get/post/...`, `axios({method})`, `$http`, Angular `HttpClient.get/...`): `"method"` on the `http_call` observation, upper-case, absent when not literal.
- The linker's `http_calls` export and the compiler's `workspace_exports.unresolved_calls` carry `method`.
- The bundle commits `workspace_exports` (the `operations` and `unresolved_calls` lists) in every tier, as one manifest-listed JSON file with its digest; `bundle_check` and `rkp verify` check it; old bundles without it still read. The publish gate's secret scan covers it.
- `solution/loader.py` reads exports from a committed bundle, so `rkp solution` links members exactly without a local compile; the "not in the committed bundle" warning stays only for old bundles.
- The TypeScript reader in `packages/bundle` reads the file and tolerates its absence.

### 8.4 Multi-repository goldens and coverage per plane (W4)

- A fixture may be multi-repository: `base/` is the solution root, holding `.rkp/solution.json` and one directory per member. `golden_check` compiles each member, loads the solution and checks a `golden.json` whose entries may name a member (`"member": "web"`) and may list identity links (`{"link": {"rule", "from", "to"}}`) and unmatched references. Single-repository fixtures are unchanged.
- F23 moves from `tests/fixtures/solution/F23-multi-repo-solution/` to `benchmarks/fixtures/F23-solution-multi-repo/` and becomes a golden; `tests/test_solution.py` reads it from there.
- Coverage per Explorer plane: each plane shows the coverage domains that bear on it (experience: `platform_constructs` of packs with UI terms; business: `transform_shapes` and rule counts by status; security: roles and permissions found), and entity counts per kind with the share confirmed where a kind has a status.

### 8.5 Web experience follow-ups (W5)

- **Per-file Symbol reference.** A relation end may be the string `"@symbol:<name>"`, meaning the top-level exported symbol `<name>` declared in the same file. The linker resolves it to that `Symbol` (`{package, qualified_name}`, as `symbol_ref` builds it); when the file exports no such symbol it falls back to `"@package"` with qualifier `component: <name>`, as today. The web pack uses it for `RENDERS`, so a screen is rendered by its component.
- **Screen key qualifier.** When one unit has more than one router or app (two `createBrowserRouter` calls, two Angular `RouterModule.forRoot`, several Next.js apps), Screen keys gain `app`: the router's variable name, or the app directory from the unit root. With a single router the key stays `{platform, name}`, so existing keys do not change.
- **Stylesheets.** An `@import` or `@use` of a literal stylesheet path is `Artifact DEPENDS_ON Artifact` (the importing file's Artifact to the imported one's, through `"@file"` and a non-declaring Artifact ref); a token defined as another token (`--a: var(--b)`, `$a: $b`, a theme key pointing to another) is `StyleToken ALIAS_OF StyleToken`.
- **Field keys for BINDS_TO.** A binding binds a `Field` only when the form's model type is a declared schema whose fields are literal (a TypeScript interface or type alias in the same file, a Zod or Yup object schema): `Field {table: "<file path>#<TypeName>", name}`, declared by the web pack with `web:ModelField` (lossy). Otherwise the binding path stays an attribute, as now.
- **Accessibility.** Per control: accessible name source (`label for`, wrapping label, `aria-label`, `aria-labelledby`, none), role, explicit `tabindex`; per screen: landmarks, heading order, positive tabindex count. Contrast ratio for a text and background token pair used together in one rule, when both values are literal colours. All attributes, plus a coverage domain `accessibility` (controls with an accessible name over controls).

### 8.6 More server pages and SAS Enterprise Guide (W6)

- Pack `jsp` (prefix `jsp`): `.jsp`, `.jspx`, `.tag`, `web.xml`; JSTL and Struts or Spring form tags. Pack `aspnet` (prefix `aspx`): Web Forms (`.aspx`, `.ascx`, `.master`, code-behind references) and Razor (`.cshtml`, `.razor`, `@page` routes). Both follow sections 1 and 2: Screens, Forms and UIControls named by section 1, `RENDERS`, `SUBMITS_TO`, `NAVIGATES_TO`, roles from `<security-constraint>`, `[Authorize(Roles=...)]` and `User.IsInRole`, behaviour units for scriptlets and code blocks only where they validate, and an honest construct list.
- SAS Enterprise Guide projects (`.egp`, a zip archive read from the file's bytes): the project as a `Program`, its process flows as `Job` and tasks as `Task` with `DEPENDS_ON` in flow order, embedded code tasks lowered by the SAS pack's existing parser, and the datasets each task names. New module `repo_intel/platforms/sas/egp.py` plus its registration in `sas/__init__.py`.

### 8.7 Workstreams (wave 2)

| | Workstream | Branch | May create or change |
|---|---|---|---|
| W1 | Transform shapes (8.1) | `claude/ir2-transforms` | `repo_intel/compile/transforms.py` (new), `repo_intel/compile/compiler.py` (transforms only), `repo_intel/knowledge/bundle.py` (table lists only), `repo_intel/knowledge/behavior_store.py`, `repo_intel/extractors/sql_lineage.py`, `repo_intel/extractors/data_python.py`, `repo_intel/platforms/sas/analysis.py` and `lower.py` (node detail only), `tests/test_transforms.py`, `benchmarks/fixtures/F24-transform-shapes/**` |
| W2 | Rules, workflows and captured screens (8.2) | `claude/ir2-rules` | `repo_intel/decisions/rules.py`, `workflows.py`, `screens.py` (new), `repo_intel/compile/pipeline.py` (wiring only), `repo_intel/compile/compiler.py` (decision results only), `repo_intel/decisions/overrides.py` (labels only, if needed), `tests/test_rules.py` |
| W3 | HTTP calls in the bundle (8.3) | `claude/ir2-http` | `repo_intel/extractors/typescript_source.py` (`http_call` only), `repo_intel/compile/link.py` (exports only), `repo_intel/compile/compiler.py` (exports only), `repo_intel/knowledge/bundle.py` (exports only), `repo_intel/knowledge/bundle_check.py`, `repo_intel/ingest/publish_secrets.py`, `repo_intel/solution/loader.py`, `packages/bundle/**`, `tests/test_http_exports.py` |
| W4 | Multi-repository goldens and coverage per plane (8.4) | `claude/ir2-goldens` | `repo_intel/compile/golden_check.py`, `benchmarks/fixtures/README.md`, `benchmarks/fixtures/F23-solution-multi-repo/**` (new), `tests/fixtures/solution/**` (removal of `F23-multi-repo-solution`), `tests/test_solution.py` (fixture path only), `tests/test_golden_multi.py`, `repo-intelligence/explorer/**` |
| W5 | Web follow-ups (8.5) | `claude/ir2-web` | `repo_intel/platforms/web/**`, `repo_intel/compile/link.py` (`@symbol:` only), `repo_intel/compile/compiler.py` (`@symbol:` only), `tests/test_platform_web.py`, `benchmarks/fixtures/F22-web-frontend/**` |
| W6 | JSP, ASP.NET and Enterprise Guide (8.6) | `claude/ir2-pages` | `repo_intel/platforms/jsp/**`, `repo_intel/platforms/aspnet/**` (new), `repo_intel/platforms/sas/egp.py` (new), `repo_intel/platforms/sas/__init__.py` (registration only), `tests/test_platform_jsp.py`, `tests/test_platform_aspnet.py`, `tests/test_platform_sas_egp.py`, `benchmarks/fixtures/F25-jsp-app/**`, `F26-aspnet-app/**`, `F27-sas-eg-project/**` |

Shared files: `compiler.py` is changed by W1, W2, W3 and W5, and `link.py` and `bundle.py` by two each; each keeps its change in its own function or a clearly separate block and touches nothing else, and the integrator merges them. A pack whose output changes raises its `version`; the integrator settles version bumps in `sas/__init__.py`. Fixture numbers are allocated above: `tests/test_fixtures.py` checks that numbering is contiguous, so on a single branch it fails while another branch's number is missing. That failure alone is expected; every other test must pass.

Everything in section 4's shared, read-only list stays read-only, and so does this document.

## 9. Wave W7, part one: designated documents and wave 2 follow-ups

Wave W7 has eight workstreams: the six data-estate packs of section 10 and the two below. They branch from the commit that adds this section, which also makes the foundation changes listed in 9.3 and 10.1.

### 9.1 Designated documents (W7.7)

An owner marks which documents the engine reads, and for which features. Nothing undesignated is read for these features.

- **The designation file** is `.rkp/documents.json`, committed by the owner:

  ```json
  {"schema": "rkp-documents/1",
   "documents": [{"path": "docs/pricing-rules.md", "features": ["rules"], "note": "signed off by finance"},
                 {"path": "specs/**/*.docx", "features": ["requirements", "schema"]}]}
  ```

  `path` is a repository path or a glob (`*`, `**`). `features` is a non-empty subset of `requirements`, `rules`, `schema` and `code_lists`. An entry with an unknown feature, or a path matching no file, is a diagnostic and is otherwise ignored. Entries are kept sorted by `path` when written. A file matched by several entries gets the union of their features. The file is a compile input, so its digest is part of the snapshot's input hash, as `.rkp/overrides.json` is.
- **Reading.** A compile step (not an extractor, since it needs the designations and the file) reads each designated file's admitted bytes. Formats: Markdown, plain text, CSV, HTML, DOCX and XLSX (zip containers read from the file's bytes, as `egp.py` does; XLSX uses cached cell values and never evaluates formulas). PDF and images are inventoried and reported as a coverage gap naming the export needed (Markdown, DOCX or text). A file is cut into blocks `{kind: heading | paragraph | item | table_row | code, text, at: [start_byte, end_byte], heading_path, cells?, sheet?, cell?}`; blocks are not stored, only cited by span as evidence.
- **requirements** (deterministic, `E2`). An item or paragraph that carries an identifier (`REQ-12`, `FR-3`, `BR-7`: two to six capitals, a hyphen, digits) or a modal statement (`shall`, `must`, `must not`, `should`) becomes `Requirement {scope: <document path>, name: <identifier lower-cased, or a slug of the first eight words, at most 60 characters>}` with attributes `statement` (at most 280 characters), `modality`, `identifier`, `section` (the heading path). `Requirement DEFINED_IN` the document's Artifact. When the statement names an existing Table, Field, Screen or Endpoint exactly (in backticks, or a qualified `schema.table`), `Requirement APPLIES_TO` it as an `E4` candidate.
- **schema** (declared, `E1`). A table whose header has a field or column name column and a type column, under a heading or with an entity column, gives `DataSchema {name: "<document path>#<entity>"}` CONTAINS `Field {table: "<document path>#<entity>", name}` with attributes `type`, `nullable`, `key`, `description` where the columns give them. Mermaid `erDiagram` blocks give the same. When exactly one Table in the unit has the entity's name (case-insensitive), the DataSchema gets `attributes.documents_table` with that Table's entity id. An unqualified entity name also matches the last part of a qualified Table name (`orders` matches `sales.orders`) when only one Table matches.
- **code_lists** (declared, `E1`). A two-column table of codes and meanings (header such as code, value, key; meaning, description, label) gives `CodeList {platform: "document", name: "<document path>#<heading slug>"}` with `attributes.values` `[{code, label}]`, at most 500 values.
- **rules** (models, `E5`, behind `CompileSettings.business_rules`). Deterministic candidates are blocks with rule signals (`must`, `must not`, `only if`, `at least`, `no more than`, `cannot`, `if ... then`, a threshold or percentage). Spec `dm.document-rules` names and groups them, following 8.2: `BusinessRule {scope: <document path>, name: <slug>}`, `status` `hypothesis` until confirmed with a `decisions` override naming `dm.document-rules`. `BusinessRule DEFINED_IN` the document's Artifact; `APPLIES_TO` as for requirements. With the switch off, candidates are counted in coverage and no rule is produced.
- **Coverage** domain `documents`, scope the unit: blocks a feature used over blocks in designated documents, with `detail.by_feature` and `detail.unreadable` (paths and the export needed).
- **Tiers.** Requirement, BusinessRule, DataSchema, Field and CodeList entities are in every tier.
- **Owner controls.** The local API serves `GET repositories/{id}/documents`, answering `{documents: [...entries], candidates: [{path, language}], problems: [...]}`, where candidates are the Artifacts whose language is one of `markdown`, `text`, `csv`, `html`, `docx`, `xlsx`, `pdf`; and `PUT repositories/{id}/documents` with `{documents: [...]}`, which validates and writes `.rkp/documents.json` the way overrides are written. The CLI has `rkp documents list [--json]` and `rkp documents set <path> --feature <f> ... | --clear`. A change takes effect at the next compile.

### 9.2 Controls and wave 2 follow-ups (W7.8)

- **Explorer.** A Documents view lists candidate documents with one checkbox per feature and saves through the API above; for designated documents it shows the counts of requirements, rules, schemas and code lists produced. The settings view gains switches for `business_rules` and `captured_screens`, saying that each sends repository content to the configured model provider. `explorer/lib/planes.ts` labels the `documents` domain and puts it on the business plane.
- **VS Code.** A command "RKP: Use this document for..." on files in the explorer context menu and the editor title menu: a multi-select quick pick of the four features, saved with `rkp documents set`, then an offer to recompile.
- **Bundle reader.** `packages/bundle/src/types.ts` declares the `Screen.captured` key of `detail_omitted` and the new entity kinds' attributes it reads.
- **Rules owned by a file.** When a rule's owner is a Symbol with no containing Program, Component or Package, `ENFORCES` comes from the Artifact of the cited candidate's path (now allowed, 9.3).
- **Form keys and `app`.** When compiler step 1b gives a unit's Screens `app`, the Forms on those Screens and every relation end naming them are rekeyed too, so two apps with a form on the same route no longer share a Form key. The UIControls on those Screens (and on their Forms) gain `app` the same way, with the relation ends naming them.
- **Web Forms roots.** A `Web.config` is an application root unless its `system.web` holds only `authorization` (with or without `location` elements). Each application root is its own `component_root`, so two Web Forms apps in one unit do not merge equal routes.
- **Language names.** `repo_intel/knowledge/profile.py` gains display names for `jsp`, `aspnet`, the W7 languages of 10.1 and the document languages.

### 9.3 Foundation changes for documents

- `Requirement` is emitted (it was reserved). `APPLIES_TO` allows `Requirement` as subject.
- `ENFORCES` allows `Artifact` as subject.
- `extractors/__init__.py` maps `.txt`, `.csv`, `.html`, `.htm`, `.docx`, `.xlsx` and `.pdf` to document languages.

## 10. Wave W7, part two: the data estate

Section "Wave W7: data estate" of [the legacy platform packs plan](legacy-platform-packs-plan.md) is binding for W7.1 to W7.6: what each pack reads and produces, its pack id and prefix, and the notes under the table. Checked against waves 1 and 2: nothing there is built yet. Teradata BTEQ scripts and T-SQL procedures stay with `sql_lineage`; W7.2's `teradata` pack reads the load and export utilities only.

### 10.1 Foundation changes (made in the opening commit)

1. **File types.** `LANGUAGES` maps `.pks`, `.pkb`, `.pls`, `.plb`, `.fnc`, `.trg` to `plsql` (`.prc` stays `jcl`; the `plsql` pack recognises an Oracle `.prc` by its content); `.fastload`, `.mload`, `.fexp` to `teradata`; `.ksh`, `.bash` to `shell`; `.bat`, `.cmd` to `batch`; `.ps1` to `powershell`; `.dtsx`, `.conmgr`, `.ispac` to `ssis`; `.dsx`, `.isx` to `datastage`; `.dml`, `.xfr` to `abinitio`; `.hql` to `hive`; `.pig` to `pig`. Generic suffixes (`.q`, `.item`, `.params`, `.xml`) are not mapped: the pack recognises its files by content or path.
2. **SQL dialects.** `sql_lineage.DIALECT` has `oracle`, `hive`, `db2` (read as generic SQL: sqlglot has no DB2 dialect), `netezza` (read as `postgres`) and `sybase` (read as `tsql`). These are also the `Program.platform` values for those engines.
3. **Transform languages.** `rkp-transform/1` accepts a registered pack id as `language`, besides `sql`, `sas` and `pyspark`.
4. **Behaviour unit kinds.** `paragraph`, `trigger` and `flow` are added.
5. **Transactions.** A scheduler-invoked entry point (and, in later waves, a CICS, IMS or SAP transaction) is an `Endpoint` with `method: "TRANSACTION"`, the transaction or entry id as `route` and `fidelity: lossy`.
6. **Record fields.** A record layout declares `Field {table: "<platform>:<layout name>", name}`, as the web pack does for model types.
7. **Triggers.** A database trigger is a `Program` with `attributes.on` `{table, events, timing}`, `READS_FROM` and `WRITES_TO` as its body shows, and `ENFORCES` any rule lifted from it. No new predicate.
8. **Binary sources.** A pack never parses a proprietary binary. The owner commits the vendor's text or XML export under `.rkp/inputs/<pack id>/`; the binary is inventoried and reported as a coverage gap naming the export needed. Zip containers of text are read from the file's bytes.

### 10.2 Workstreams (wave W7)

| | Workstream | Branch | Fixture | May create or change |
|---|---|---|---|---|
| W7.1 | Oracle PL/SQL | `claude/ir7-plsql` | F28-oracle-plsql | `repo_intel/platforms/plsql/**`, `tests/test_platform_plsql.py` |
| W7.2 | Warehouse utilities and SQL in shell | `claude/ir7-warehouse` | F29-warehouse-shell | `repo_intel/platforms/teradata/**`, `dbproc/**`, `shell/**`, `tests/test_platform_teradata.py`, `test_platform_dbproc.py`, `test_platform_shell.py` |
| W7.3 | SSIS and Informatica | `claude/ir7-etl-ms-infa` | F30-ssis-package, F31-informatica-export | `repo_intel/platforms/ssis/**`, `informatica/**`, their test files |
| W7.4 | DataStage, Talend and Ab Initio | `claude/ir7-etl-ibm-talend` | F32-datastage-job, F33-talend-job | `repo_intel/platforms/datastage/**`, `talend/**`, `abinitio/**`, their test files |
| W7.5 | Hadoop | `claude/ir7-hadoop` | F34-hadoop-oozie | `repo_intel/platforms/hadoop/**`, `tests/test_platform_hadoop.py` |
| W7.6 | Batch schedulers | `claude/ir7-scheduler` | F35-scheduler-exports | `repo_intel/platforms/scheduler/**`, `tests/test_platform_scheduler.py` |
| W7.7 | Designated documents (9.1) | `claude/ir7-documents` | F36-designated-documents | `repo_intel/documents/**` (new), `repo_intel/compile/pipeline.py` (the documents step only), `repo_intel/compile/compiler.py` (writing document results only), `repo_intel/knowledge/local_api.py` (the documents endpoints only), `repo_intel/cli.py` (the `documents` command only), `tests/test_documents.py` |
| W7.8 | Controls and wave 2 follow-ups (9.2) | `claude/ir7-controls` | none (F22 and F26 goldens change) | `explorer/**`, `packages/vscode-extension/**`, `packages/bundle/src/types.ts`, `repo_intel/decisions/rules.py`, `repo_intel/compile/compiler.py` (`_screen_apps` only), `repo_intel/platforms/aspnet/**`, `repo_intel/compile/link.py` (`link_app_relative_routes` only), `repo_intel/knowledge/profile.py`, `benchmarks/fixtures/F22-web-frontend/**`, `F26-aspnet-app/**`, and their tests |

Each fixture directory is created by its workstream under `benchmarks/fixtures/`. The contiguous-numbering test may fail on a single branch only because another branch's fixture is missing. Pack versions start at 1.0.0; W7.8 raises the `aspnet` and `web` versions it changes.

Shared files: `compiler.py` is changed by W7.7 and W7.8, each in its own block. `sql_lineage.py`, `transforms.py`, `behavior.py`, `ontology.py`, `extractors/__init__.py` and everything in section 4's read-only list stay read-only; a pack calls the shared SQL recognizer (`transforms.sql_query`, `sql_lineage`'s scanner) without changing it. A missing kind, relation or language is a request for the foundation, not a change.

### 10.3 Foundation changes after the W7 review

1. **Per-file supersession.** An extractor may set `supersedes_when_parsed = ("<extractor id>", ...)`. For a file on which it returns at least one observation, the observations and diagnostics of the extractors it names are dropped; on a file it does not read, theirs stand. `plsql.source` names `data.sql_lineage`, so a `.sql` file read as PL/SQL is not also read as generic SQL. `teradata.utilities` names `data.sql_lineage`, `hadoop.hive` names it and `data.sql_ddl`, and `dbproc.routines` names both and `plsql.source`; so a file gives one Program, under its engine, and no `ALIAS_OF` joins it to a generic reader's guess. Since a superseding reader drops the others' output, it reads only files that are surely its own: `plsql.source` takes a `.sql` procedure or function only with a sign only Oracle gives (`OR REPLACE`, `IS`, a `/` line, `%TYPE`, `:=`, `DBMS_`, `EXCEPTION WHEN`, `VARCHAR2`, `END <name>;`), so a bare `CREATE PROCEDURE p AS BEGIN ... END;` stays generic SQL, and it leaves a file with DB2 markers (`MODE DB2SQL`, `LANGUAGE SQL`, `BEGIN ATOMIC`, `SPECIFIC`, `--#SET TERMINATOR`) to `dbproc`.
2. **Kinds.** `CONSUMES` allows `Job` and `Task` as subject (a scheduler Job CONSUMES the `TRANSACTION` Endpoint it starts); `EXPOSES` allows `Job` (a scheduled Job EXPOSES its `TRANSACTION` Endpoint). A construct may be `modelled_as` `transform:<op>`, naming an op of `rkp-transform/1`.
3. **Shapes from behaviour.** `transforms.from_behavior` reads a unit's queries in its engine's dialect (`plsql` as `oracle`, `hive`, `teradata`, `netezza` as `postgres`, `sybase` as `tsql`, `db2` as generic SQL) and labels the shapes with the unit's language when it is a registered pack id, else `sql`.
4. **Tables over a database link.** An Oracle table read or written over a database link (`orders@sales_link`) is `Table {datastore: <db link name>, name}`, beside the `{datastore: "default", name}` of a local table.
5. **Paths a scheduler names.** Paths that a scheduler or job definition names (a script to run, a file to wait for, a log) may appear as written in attributes: they name the repository's or the target system's files, not the engine host's own paths, which section 6 item 7 still keeps out.
6. **Script targets.** A job step that runs a script links it twice, both with `declare=False`: `RUNS Artifact` for the file, and `RUNS Program {platform: P, name: <repository path candidate>}`, where P is `shell` for `.sh`, `.ksh`, `.bash`, `.bat`, `.cmd` and `.ps1`, `hive` for `.hql` and `.q`, `sas` for `.sas`, and `pig` for `.pig`. Each links only when the repository defines that Program.
7. **SQL lineage.** `sql_lineage` selects `teradata` for `REPLACE PROCEDURE` only at the start of a line, so Oracle `CREATE OR REPLACE PROCEDURE` stays generic SQL, and resolves a T-SQL `UPDATE <alias> SET ... FROM <table> AS <alias>` to the table the FROM clause binds.
8. **Secret detectors.** `oracle_identified_by` (`IDENTIFIED BY <password>`, not `IDENTIFIED BY VALUES`) and `parameter_file_credential` (an Informatica parameter file line `$$<name>=<value>` whose name says password, passwd, pwd or secret). Neither flags a reference to another value (`&x`, `:x`, `$x`) or a short or plain-word value.

## 11. Wave W8: mainframe and midrange

Section "Wave W8: mainframe and midrange" of [the legacy platform packs plan](legacy-platform-packs-plan.md) is binding for W8.1 to W8.6: what each pack reads and produces, its pack id and prefix, and the notes under the table. Checked against waves 1, 2 and W7: `legacy.cobol` and `legacy.jcl` read structure only (programs, calls, copybooks, embedded SQL, CICS files, job steps and DD datasets) and are superseded here; W7.6's `scheduler` links to JCL; nothing else covers these platforms.

### 11.1 Foundation changes (made in the opening commit) and shared keys

1. **File types.** `LANGUAGES` maps `.bms` to `bms`; `.mfs`, `.dbd`, `.psb` to `ims`; `.rpg`, `.rpgle`, `.sqlrpgle` to `rpg`; `.clp`, `.clle` to `cl`; `.pf`, `.lf`, `.dspf`, `.prtf` to `dds`; `.pli`, `.pl1` to `pli`; `.ezt` to `easytrieve`; `.rexx`, `.rex` to `rexx`; `.clist` to `clist`; `.asm`, `.hlasm`, `.mac` to `hlasm`; `.nsp`, `.nsn`, `.nss`, `.nsm`, `.nsl`, `.nsh`, `.nsc`, `.nsa`, `.nsg`, `.nsd` to `natural`. `.cbl`, `.cob`, `.cobol`, `.cpy`, `.jcl`, `.prc` and `.proc` keep their mappings. IDMS schema and subschema source, CICS CSD exports, DB2 DDL, DCLGEN and `BIND` cards have no suffix of their own: their pack recognises them by content, or by path under `.rkp/inputs/<pack id>/`. A BMS map is assembler macro source: `bms` recognises `DFHMSD` in any of `.bms`, `.mac`, `.asm` and names `hlasm.source` in `supersedes_when_parsed` (10.3 item 1), so a map is not also counted as assembler.
2. **Executable programs.** A load module on z/OS or a `*PGM` object on IBM i, whatever its language (COBOL, PL/I, assembler, Easytrieve, RPG, CL), is `Program {platform: "mainframe", name: <upper-cased name>}`, the key `legacy.cobol` and F14 use, with `attributes.language` naming the language. A caller rarely knows the callee's language, so this is the one key every `CALL`, `LINK`, `XCTL`, `EXEC PGM=`, `RUN PROGRAM`, `CALL PGM()` and `SBMJOB` uses. Other units keep their platform: a REXX exec or CLIST is `Program {platform: "tso", name: <member>}` (run through `IKJEFT01` `%NAME`, `EX` or `EXEC`); a Natural object is `Program {platform: "natural", name}`; a JCL procedure stays `Job {framework: "jcl", name}`.
3. **Copybooks and record layouts.** A COBOL copybook stays `DataSchema {kind: "copybook", name: <member>}`; PL/I `%INCLUDE` members are `DataSchema {kind: "include", name: <member>}`; a program's own record (an `FD` or `WORKING-STORAGE` 01-level it does not copy) is not a DataSchema. Fields follow 10.1 item 6 with the pack id as platform: `Field {table: "cobol:<copybook>", name}`, `Field {table: "pli:<member>", name}`, and so on. A field name is its data name; a name repeated in one layout is qualified by its parent (`<parent>.<name>`); `FILLER` is not declared. Attributes: `level`, `pic`, `usage` (`COMP-3`, `BINARY` ...), `occurs`, `redefines`, `offset` and `length` in bytes when every field before it has a fixed length.
4. **Code lists from conditions.** A COBOL 88-level, a DDS `VALUES` keyword and an Adabas or IDMS value list are `CodeList {platform: <pack id>, name: "<layout>.<field>"}` with the values as `attributes.values` (sorted as written, at most 200, then `truncated: true`). `BINDS_TO` now allows `Field` as subject: the `Field BINDS_TO` its `CodeList`.
5. **Datasets by DD name.** A COBOL `SELECT f ASSIGN TO DDNAME` (or PL/I `DCL f FILE` with a title, or an RPG F-spec on z/OS) cannot know the dataset: the JCL that runs the program binds the DD name. The language pack emits a `dd_file` observation `{program: <Program ref>, dd, mode: "input" | "output" | "i-o" | "extend" | null, select, record}` (mode from the `OPEN` statements, null when there is none) and no relation. The `zos` pack emits, per DD statement of a step that runs a program, a `dd_binding` observation `{task: <Task ref>, program: <Program ref>, dd, dataset: <Dataset ref>, disp}` beside the Task `READS_FROM`/`WRITES_TO` it already gives. W8.2's linker pass `link_dd_datasets` joins them: for a `dd_file` and a `dd_binding` with the same program and DD name, `Program READS_FROM` (mode input) or `WRITES_TO` (output, extend; both for i-o; DISP decides when the mode is null) the Dataset, evidence `E3`, qualifier `via: {job, step}`. A DD name bound in no job stays an attribute of the program (`attributes.files`).
6. **Transactions.** CICS and IMS transactions follow 10.1 item 5 with the key W7.6 already emits: `Endpoint {component_root: "@unit", protocol: "cics" | "ims", method: "TRANSACTION", route: <transaction id, upper case>}`. The program a CSD `DEFINE TRANSACTION ... PROGRAM(P)` or an IMS `TRANSACT` names `EXPOSES` it (`Program {platform: "mainframe", name: P}`, `declare=False`).
7. **Screens.** A BMS map is `Screen {platform: "bms", name: "<mapset>.<map>"}` with a `Form {screen: "<mapset>.<map>", name: <map>}` and `UIControl {container: "<mapset>.<map>", name: <field label>}`; an unnamed field is not declared and is counted. A program's `EXEC CICS SEND MAP('M') MAPSET('S')` `RENDERS` that Screen (`declare=False`) when both names are literal; `RECEIVE MAP` gives `Form SUBMITS_TO Program` from the `bms` side only when the CSD names the program, else the COBOL Program carries `attributes.maps`. IMS MFS formats are `Screen {platform: "ims", name: <MID or MOD name>}`; IBM i display files `Screen {platform: "ibmi", name: "<file>.<record format>"}`; Natural maps `Screen {platform: "natural", name}`.
8. **Hierarchical and network records.** An IMS segment is `Table {datastore: "ims:<DBD name>", name: <segment>}`, its parent `CONTAINS` it; an IDMS record is `Table {datastore: "idms:<schema>", name: <record>}`, and a set is `attributes.sets` on its owner record `[{set, member, order}]`; an Adabas DDM is `Table {datastore: "adabas", name: <DDM name>}`. All three are lossy. Their fields are `Field {table: "ims:<DBD>.<segment>", name}`, `Field {table: "idms:<schema>.<record>", name}` and `Field {table: "adabas:<DDM name>", name}`, each `CONTAINS`ed by its Table. A PSB names its program: the `ims` pack gives `Program {platform: "mainframe", name: <PSBNAME>}` (`declare=False`) `READS_FROM` and, when `PROCOPT` allows insert, replace or delete, `WRITES_TO` each sensitive segment, qualifier `pcb` and `procopt`. COBOL and PL/I `CBLTDLI`/`PLITDLI` calls are constructs, with the segment names of literal SSAs in `attributes.dli_segments`.
9. **Physical and logical files on IBM i.** A physical file is the table SQL names by the same name: `Table {datastore: "default", name}` and `Field {table, name}` normalised exactly as `data.sql_ddl` (`contracts_data.py`) normalises a `CREATE TABLE` and its columns, so DDS, SQL DDL and embedded SQL name one table (term `ibmi:PhysicalFile`, lossy); a logical file is `DataSchema {kind: "ibmi-lf", name: <file>}` that `CONTAINS` the Fields it selects and whose `attributes.based_on` names the physical files; a printer file is `Report {platform: "ibmi", name: <file>}`.
10. **JCL members.** A scheduler names the library member it submits (`Job {framework: "jcl", name: <member>}`, `declare=False`, from W7.6), while a JCL Job is keyed by its `JOB` statement's name. When the two differ, `zos` declares `Job {framework: "jcl", name: <member>}` (term `zos:Member`, lossy) `ALIAS_OF` the job, so the scheduler's `RUNS` links.
11. **Transform shapes.** DFSORT and ICETOOL steps, Easytrieve `SORT` and the like emit shapes with `language` the pack id (10.1 item 3), `E1` evidence, ops from `rkp-transform/1` (`sort`, `filter` for `INCLUDE`/`OMIT`, `project` for `INREC`/`OUTREC` field lists, `aggregate` for `SUM`).
12. **Embedded SQL.** `EXEC SQL` in COBOL, PL/I, RPG (`EXEC SQL` and `/EXEC SQL`) and Natural (`SELECT ... END-SELECT` with SQL DDMs) goes through the shared scanner with the `db2` dialect, after blanking host variables to keep offsets, as `legacy.cobol` does; tables stay `{datastore: "default", name}` so they join with DDL and with `sql_lineage`. In behaviour units the statement is a `query` node with `host_vars` listing the host variables read and set.

### 11.2 Workstreams (wave W8)

| | Workstream | Branch | Fixture | May create or change |
|---|---|---|---|---|
| W8.1 | COBOL | `claude/ir8-cobol` | F37-cobol-batch-online | `repo_intel/platforms/cobol/**`, `tests/test_platform_cobol.py` |
| W8.2 | z/OS job and resource definitions | `claude/ir8-zos` | F38-zos-definitions | `repo_intel/platforms/zos/**`, `tests/test_platform_zos.py`, `repo_intel/compile/link.py` (`link_dd_datasets` and its one call in the linker sequence only) |
| W8.3 | Mainframe screens and IMS | `claude/ir8-screens-ims` | F39-bms-ims | `repo_intel/platforms/bms/**`, `ims/**`, `tests/test_platform_bms.py`, `test_platform_ims.py` |
| W8.4 | IBM i | `claude/ir8-ibmi` | F40-ibmi-rpg | `repo_intel/platforms/ibmi/**`, `tests/test_platform_ibmi.py` |
| W8.5 | Other mainframe languages | `claude/ir8-zlang` | F41-pli-easytrieve | `repo_intel/platforms/pli/**`, `easytrieve/**`, `rexx/**`, `hlasm/**`, their test files |
| W8.6 | Natural, Adabas and IDMS | `claude/ir8-natural-idms` | F42-natural-adabas | `repo_intel/platforms/natural/**`, `idms/**`, their test files |

`cobol` supersedes `legacy.cobol` and `zos` supersedes `legacy.jcl` (the pack's `supersedes`), and each emits at least what the extractor it replaces emitted, with the same kinds and natural keys: F14 keeps passing with its golden unchanged (rule 3 of section 2). A pack may import helpers from `repo_intel/extractors/legacy.py` inside functions but does not change it. Pack versions start at 1.0.0. Fixtures are created by their workstream under `benchmarks/fixtures/`; the contiguous-numbering test may fail on a single branch only because another branch's fixture is missing. A fixture that needs another workstream's files to show a link (a COBOL program in F38 for a DD join, a BMS map in F37) includes small sources of its own and asserts only what its own pack emits plus links that resolve once both packs run, which they do on every branch for packs already on main and on the merged branch for the others.

Shared files: `link.py` is changed by W8.2 only, in its own function. `extractors/__init__.py`, `legacy.py`, `sql_lineage.py`, `transforms.py`, `behavior.py`, `ontology.py`, the W7 packs and everything in section 4's read-only list stay read-only. A missing kind, relation, unit kind or language is a request for the foundation, not a change.

### 11.3 Foundation changes after the W8 review

1. **One case for SQL names.** `data.sql_ddl` lower-cases table and column names, as `sql_lineage` always has, so a `CREATE TABLE ACCT.ACCOUNT`, a DDS physical file `ACCOUNT` and an embedded `SELECT ... FROM ACCT.ACCOUNT` name one `Table {datastore: "default", name: "acct.account"}`. Items 9 and 12 of 11.1 mean lower case by "normalised as `data.sql_ddl`"; every pack that reads DDL (the `zos` pack's DB2 DDL included) does the same.
2. **Linear spans.** `extractors.base.span` finds lines by bisecting the file's line starts, computed once per file, instead of counting newlines from byte 0 for each span. Output is unchanged.
3. **Secret detectors.** `cobol_move_credential` (a quoted literal `MOVE`d to a data item named for a password, secret or token), `cobol_value_credential` (such an item's `VALUE`), `pli_init_credential` (its PL/I `INIT`) and `cl_password_keyword` (`RMTPWD(...)`, `PASSWORD(...)`, `PWD(...)` in CL), each checked like `oracle_identified_by`. A pack still blanks any literal it would copy from a credential-named item, including a condition-name value, because admission flags only values that look like secrets.
4. **DCLGEN members** are copybooks: `DataSchema {kind: "copybook", name: <member>}` with term `zos:Dclgen`, so `EXEC SQL INCLUDE <member>` and `COPY <member>` link to them.
5. **In-stream procedures.** An exception to the `legacy.jcl` superset, by design: steps of an in-stream PROC are `Task {framework: "jcl", job: <proc name>, name}` under the procedure's Job, and the calling step's overrides apply to them; `legacy.jcl` keyed them under the file's first JOB.
6. **Keys the packs settled.** Paragraphs, sections, subroutines and internal procedures are `Symbol {platform: "mainframe", program: <program>, name}` (COBOL, RPG, PL/I, Easytrieve) and `Symbol {platform: "natural", program, name}`; `DataSchema` kinds are `copybook`, `include` (PL/I), `easytrieve-file`, `ibmi-lf`, `natural-lda`, `natural-pda`, `natural-gda`, `idms-schema` and `idms-subschema`.
7. **Explorer.** `bms`, `ims`, `ibmi`, `easytrieve` and `natural` are UI platforms in `explorer/lib/plane-coverage.ts`.
8. **Deferred.** Joining DD names across compilation units; a linker pass joining Natural views defined in a data area to the DDM they read; a range expression in the behavioural IR; Form `SUBMITS_TO` from a CICS `RECEIVE MAP`, which needs the map and the CSD together.
