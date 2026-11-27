# Benchmark fixtures

Small synthetic repositories the compiler is tested against. Each is `base/`
plus ordered overlays under `commits/`, with a `golden.json` saying what the
compiler must produce. `repo_intel/compile/fixtures.py` turns one into a real Git
repository with stable commit SHAs, and `python -m repo_intel.compile.golden_check`
compares a compile with the golden.

## The golden

| Key | Meaning |
|---|---|
| `entities` | Entities a correct compile of the base declares: `kind`, `natural_key`, optional `attributes` (each must equal the compiled value), `since` (the milestone from which it is due) |
| `edges` | Assertions: `subject` and `object` (each `kind` and `natural_key`), `predicate`, `since` |
| `absent_entities` | Entities the base must not declare, each with `why` |
| `forbidden_kinds` | Kinds no commit may declare |
| `commits` | One entry per overlay under `commits/`, in order: `dir`, `expect`, and the `added_entities` and `removed_entities` that commit causes |
| `exclusions`, `oversize_not_parsed` | Paths the inventory must report as excluded or too large to parse |
| `granularity` | The deployment granularity label the fixture's design implies, or `null` |
| `expected_unknowns`, `expected_conflicts`, `secret_probes`, `decision_cases` | Read by the tests that need them |

## Multi-repository fixtures

A fixture whose `base/` holds `.rkp/solution.json` (the `rkp-solution/1` format of a
solution's `.rkp/solution.json`) is multi-repository. `base/` is the solution root, laid
out as an owner would lay it out: the manifest, the solution's inputs under
`.rkp/inputs/`, and one directory per member at the `path` the manifest gives it. Members
must be repositories (`path`), not delivered bundles. F23 is the first.

`golden_check` materializes the fixture as usual, then for each commit copies every
member's directory into a Git repository of its own, compiles it into its bundle, loads
the solution from those bundles and resolves identity across them
(`repo_intel.solution`). The golden is read as above, with these differences:

- Any entity, absent entity, edge or commit entry may name a member: `"member": "web"`.
  The solution's own inputs are the member `inputs`. An entry that names a member must be
  in that member; one that does not may be in any member. An edge is checked within one
  member, both ends in it.
- `links`: identity links and candidates across members, each
  `{"link": {"rule", "from", "to"}, "since"}`, where `from` and `to` are
  `{"member", "kind", "natural_key"}` and `rule` is a rule of
  `repo_intel/solution/identity.py` (`same_table`, `endpoint_path_unique`,
  `endpoint_method_path`, `config_key_name_only`, ...). The rule's name says whether it is exact (E3) or a
  candidate (E4).
- `absent_links`: links that must not be found, each with `why`, for example a shared
  configuration key name that must stay a candidate rather than an exact link.
- `unmatched`: references no member resolves, `{"member", "kind", "reference", "reason", "since"}`
  as `rkp solution links` reports them (`http_call`, `no_endpoint`).
- `solution_coverage`: `{"domain", "scope", "numerator", "denominator", "since"}`, each
  matching the resolution's coverage entry exactly (`solution_data_refs`, `solution_http_calls`).
  A commit entry may carry its own `solution_coverage`, checked against that commit.
- A commit entry may also list `added_links`, `removed_links`, `added_unmatched` and
  `removed_unmatched`.
- A problem the loader reports for a member or input fails the check.

`tests/test_fixtures.py` checks a multi-repository golden against the member-by-member
compile, made once per test session, so `golden_check.check` does not compile the members
a second time. Entities in `entities` must be unique by kind and natural key within a
member; the same key may appear in two members (a table in the service and in the
catalogue). Granularity is decided per member, so the whole-tree granularity test skips
these fixtures. The whole tree is still compiled as one repository for replay parity,
forbidden kinds, `absent_entities` without a member and items due at `M0`, so
multi-repository goldens use `M2` or later for member entries.

## Planted test data: nothing here is real

Some fixtures contain adversarial content on purpose. It exists so the tests
can prove the compiler handles it safely.

| What | Where | Why |
|---|---|---|
| Fake AWS access key ID (`AKIAIOSFODNN7EXAMPLE`, the example value from AWS documentation) | `F01-python-api/base/.env.example`, repeated in that fixture's `golden.json` under `secret_probes` and in `tests/test_secrets.py` | Must be detected at admission and never appear in compiler output or evidence excerpts |
| Made-up Anthropic-style key (`sk-ant-fixture-EXAMPLE-000…`) | `F08-ai-agent/base/.env.example`, repeated in its `golden.json` | Same |
| Prompt-injection text addressed to AI tools | `F01` and `F06` READMEs, `F08` `prompts/system.md`, a file name in `F10` | Must be treated as data and change nothing |
| Symlinks pointing outside the repository | `F01` and `F10` (created when the fixture is materialized) | Must be recorded and never followed |
| A submodule entry with a dummy commit ID | `F10` | Must be recorded and never fetched |
| A DataFrame passed in as a parameter and a table named from an f-string (`silver.orders_{region}`) | `F24-transform-shapes/base/jobs/archive_orders.py` | Must stay `{placeholder}` names in a transform marked dynamic, never become a `Dataset` or `Table` (the golden's `absent_entities` and `expected_unknowns`) |
| Made-up password (`fixture-not-a-real-password`) as the value of the context parameter `mail.password` | `F25-jsp-app/.../WEB-INF/web.xml`, in the base and every commit; repeated in its `golden.json` under `secret_probes` | Must be detected at admission (`servlet_param_value`) and never appear in compiler output; only the parameter is counted (`tests/test_secrets.py`, `tests/test_platform_jsp.py`) |
| An include target read from a request parameter (`${param.extra}`) and a link to another host with a query string | `F25-jsp-app/base/src/main/webapp/reports/sales.jsp` and `index.jsp` | The include stays unresolved with a diagnostic; the link keeps scheme and host only |
| Made-up password (`fixture-not-a-real-password`) in a connection string | `F26-aspnet-app/base/desk/Web.config` and `commits/02-auditor-role/desk/Web.config`; repeated in its `golden.json` under `secret_probes` | The password must be detected at admission (`connection_string_password`); connection string, app setting and host values must never appear in compiler output (`tests/test_secrets.py`, `tests/test_platform_aspnet.py`) |
| A `Server.Transfer` to a value read from the query string | `F26-aspnet-app/base/desk/Orders/Edit.aspx` | The target stays unresolved with a diagnostic |
| Made-up database password (`fixture-not-a-real-password-0001`) in an embedded code task's `LIBNAME` | `F27-sas-eg-project/eg/sales_reporting.egp` in the base and every commit (a zip archive), written by `F27-sas-eg-project/make_egp.py`; repeated in its `golden.json` under `secret_probes` | Must be detected at admission and never appear in compiler output (`tests/test_secrets.py`, `tests/test_platform_sas_egp.py`) |
| A code task that runs a file on an analyst's machine (`C:\Users\analyst\reports\monthly.sas`) | `F27-sas-eg-project` | The file is not in the repository: it must not be invented, and a diagnostic says so |
| A JCL procedure with the Oracle `.prc` suffix, and dynamic SQL that names its tables from a variable | `F28-oracle-plsql/base/jcl/NIGHTLY.prc` and `db/packages/order_api.pkb` (`archive_orders`) | The JCL member must not be read as Oracle; the dynamic statements are counted with a diagnostic and no table is guessed (the golden's `absent_entities` and `expected_unknowns`) |
| A table reached across a database link through a synonym | `F28-oracle-plsql/base/db/install/grants_and_jobs.sql` and the MERGE in `order_api.pkb` | The table is in the link's datastore, never a local table named with the link; the link's connect string is not copied |
| Made-up database password (`fixture-not-a-real-password-0028`) in a database link's `IDENTIFIED BY "..."` | `F28-oracle-plsql/base/db/install/grants_and_jobs.sql` and `commits/03-retire-cart-purge/`; repeated in its `golden.json` under `secret_probes` | Must be detected at admission (`oracle_identified_by`) and never appear in compiler output; the link (`dblink:rpt`, a fixed user) is declared without its user, password or connect string |
| Made-up database password (`fixture-not-a-real-password-0029`) assigned to `DB_PASSWORD` | `F29-warehouse-shell/base/etl/nightly_orders.ksh`, passed on to `sqlplus` and a BTEQ `.LOGON` | Must be detected at admission and never appear in compiler output; the logons are read for their tools, not their credentials |
| Made-up password (`fixture-not-a-real-password-0002`) in a `;Password=...;` connection string | `F30-ssis-package/base/etl/SalesLoad/Warehouse.conmgr`; repeated in its `golden.json` under `secret_probes` | Must be detected at admission (`connection_string_password`); the connection string and host are never copied |
| A statement taken from a variable and a destination named by a variable | `F30-ssis-package/base/etl/SalesLoad/Publish.dtsx` (`User::IndexSql`, `User::MartTable`) | No statement or table is guessed: the target stays dynamic with a diagnostic |
| Made-up password (`fixture-not-a-real-password-0003`) in an `sftp://` URL on a command task's command line | `F31-informatica-export/base/informatica/exports/sales_folder.xml`, in the base and commits 02 and 03; repeated in its `golden.json` under `secret_probes` | Must be detected at admission (`url_credentials`) and never appear in compiler output; the command is not read |
| Parameter values for connections and dates | `F31-informatica-export/base/informatica/params/wf_daily_sales.par` | Parameters are counted or linked by name; their values are never read |
| A file path built from a job parameter, job parameter defaults, a server name and mail addresses | `F32-datastage-job/base/datastage/export/LoadCustomers.dsx` and `NightlyLoad.dsx` | The path stays dynamic with a diagnostic; defaults, the server and the addresses are never copied |
| A file named from a context variable; context values, hosts, users, encrypted passwords and mail addresses | `F33-talend-job/base/DWH/process/` items | The name stays dynamic with a diagnostic; none of the values is copied |
| Made-up password (`fixture-not-a-real-password-0004`) in a `jdbc:` URL in a Sqoop command | `F34-hadoop-oozie/base/apps/daily_orders/workflow.xml` and `commits/03-retire-pig/`; repeated in its `golden.json` under `secret_probes` | Must be detected at admission (`url_credentials`) and never appear in compiler output; the connection string and host are never copied |
| Paths and table names built from parameters (`$INPUT`, `${hivevar:mart_db}`, `${appBaseDir}`) | `F34-hadoop-oozie/base/apps/daily_orders/` | They stay parameterized, marked dynamic, with a diagnostic; parameter values are never substituted |
| A member set by AutoEdit, a condition no job adds, and scripts at paths outside the repository (`$HOME/bin/...`, `/opt/...`) | `F35-scheduler-exports/base/.rkp/inputs/scheduler/` | Nothing is linked or invented for them; diagnostics say so |
| Undesignated documents that read like requirements, a code table and a field table; a PDF designated for rules | `F36-designated-documents/base/docs/meeting-notes.md`, `glossary.md`, `scanned-policy.pdf` | Nothing is read from an undesignated document; the PDF is reported as a gap naming the export needed |
| Made-up database password (`fixture-not-a-real-password-0037`) MOVEd to `WS-DB-PASSWD`, and a program called through `WS-RULE-PGM`, which is given two literals | `F37-cobol-batch-online/base/cobol/ACCTPOST.cbl` and `commits/01-fee-rule/`; the password repeated in its `golden.json` under `secret_probes` | The password is flagged at admission and never reaches output; the two-literal call stays dynamic |
| JOB statement accounting fields, a DB2 subsystem name and IMS and stage-1 operands | `F38-zos-definitions`, `F39-bms-ims` | Names are kept where 11.1 says, values never |
| Made-up password (`fixture-not-a-real-password-0040`) assigned in an RPG program | `F40-ibmi-rpg/base/qrpglesrc/CUSTUPD.sqlrpgle`; repeated in its `golden.json` under `secret_probes` | Must never reach output |
| Made-up password (`fixture-not-a-real-password-0041`) assigned in a REXX exec | `F41-pli-easytrieve/base/rexx/RUNRPT.rexx`; repeated in its `golden.json` under `secret_probes` | Must never reach output |
| Made-up password (`fixture-not-a-real-password-0042`) assigned in a Natural program | `F42-natural-adabas/base/natural/EMPMAINT.nsp` and `commits/02-salary-history/`; repeated in its `golden.json` under `secret_probes` | Must never reach output |

These paths are excluded from GitHub secret scanning in
`.github/secret_scanning.yml`. If another scanner flags them, they are safe to
allow as test data. Do not put real credentials in a fixture.
