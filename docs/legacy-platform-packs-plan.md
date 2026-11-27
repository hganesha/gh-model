# Legacy platform packs: waves W7 to W10

Status: 2026-11-26. Waves 1 and 2 are built; wave W7 is folded into sections 9 and 10 of the contract and wave W8 into section 11, which bind them. Waves W9 and W10 are proposed. It follows the format of the [solution IR contract](solution-ir-contract.md), which records waves 1 and 2, and is written to be folded into that contract as one section per wave. Sections 1 to 7 of the contract apply to every workstream here.

A workstream is named by its wave and its place in it: `W8.1` is the first workstream of wave W8. The contract restarts W1 to W6 inside each wave; the wave prefix keeps the names apart.

Only waves 1 and 2 are written down where this plan could read them. Check each wave below against waves W3 to W6 before it opens, and drop or narrow any workstream they already cover.

## What is already covered, and so not planned here

| Area | Where |
|---|---|
| COBOL, JCL, Teradata BTEQ and procedures, T-SQL procedures, Synapse and Data Factory | `repo_intel/extractors/legacy.py`, `sql_lineage.py` (structure only, no behaviour) |
| ColdFusion, SAS | packs `cfml`, `sas` (wave 1) |
| HTML, JSX/TSX, Vue, Angular, CSS | pack `web` (waves 1 and 2) |
| JSP, ASP.NET Web Forms and Razor, SAS Enterprise Guide | wave 2, W6 |
| Database catalogue dumps | pack `catalog` |

## Order

| Wave | Theme | Workstreams | Why here |
|---|---|---|---|
| W7 | Data estate | W7.1 to W7.6 | The most common migrations are of databases, ETL and warehouses; most sources are declarative exports, so evidence is `E1`; transform shapes (8.1) apply directly |
| W8 | Mainframe and midrange | W8.1 to W8.6 | Finishes what `legacy.cobol` and `legacy.jcl` started; needs schedulers (W7.6) to link jobs |
| W9 | Application platforms | W9.1 to W9.6 | Builds on the `jsp` and `aspnet` packs and on the SQL dialects from wave W7 |
| W10 | Packaged platforms, reporting, and findings | W10.1 to W10.5 | Lower demand per platform; W10.4 and W10.5 read what every earlier pack produces |

Six workstreams run in parallel per wave, as in waves 1 and 2. W10.4 can be pulled forward to follow wave W8 if migration findings are wanted sooner.

## Foundation changes before wave W7

The integrator makes these in the commit that opens wave W7. They are in shared, read-only files, so no workstream makes them.

1. **File types.** `extractors/__init__.py` `LANGUAGES` gains the suffixes each pack below reads.
2. **SQL dialects.** `sql_lineage.DIALECT` gains `oracle`, `db2`, `hive`, `netezza` (read as `postgres`) and `sybase` (read as `tsql`). `Program.platform` gains the matching values.
3. **Transform languages.** `rkp-transform/1` accepts a pack id as `language` (today `sql`, `sas`, `pyspark`), so an ETL mapping or a DFSORT step is described with the same vocabulary.
4. **Behaviour unit kinds.** Add `paragraph` (COBOL, RPG subroutine), `trigger` and `flow` (an integration or ETL flow) to the unit kinds of section 3.
5. **Transactions.** A CICS or IMS transaction, an SAP transaction code and a scheduler-invoked entry point are `Endpoint`s with `method: "TRANSACTION"` and the transaction id as route (fidelity `lossy`).
6. **Record fields.** A copybook, DDS or DCLGEN layout declares `Field {table: "<platform>:<layout name>", name}`, as the web pack does for model types (8.5).
7. **Triggers.** Decide the relation from a database trigger to its table. Recommended: the trigger is a `Program` with `attributes.on` naming the table and event, and `ENFORCES` any rule lifted from it; no new predicate.
8. **Binary sources.** A pack never parses a proprietary binary (`.pbl`, `.fmb`, `.rpt`, `.unx`, Ab Initio `.mp`). The owner commits the vendor's text or XML export under `.rkp/inputs/<pack id>/`; the binary itself is inventoried and reported as a coverage gap that names the export needed. Zip containers of text (`.egp`, `.ispac`, `.dsx` archives) are read from the file's bytes, as `egp.py` does.

## Wave W7: data estate

Each workstream branches from the commit that opens its wave and owns `repo_intel/platforms/<id>/**`, `tests/test_platform_<id>.py` and its fixture, and nothing else unless listed.

| | Workstream | Packs (prefix) | Reads | Produces | Branch | Fixture |
|---|---|---|---|---|---|---|
| W7.1 | Oracle PL/SQL | `plsql` (`ora`) | `.pks`, `.pkb`, `.pls`, `.plb`, `.prc`, `.fnc`, `.trg`, and `.sql` that creates a package, procedure, function or trigger | Packages as `Program`s that CONTAIN `Symbol`s; behaviour per procedure, function and trigger; table reads and writes; database links and synonyms (`ALIAS_OF`); `DBMS_SCHEDULER` and `DBMS_JOB` as `Job`s; `GRANT` as `Role GRANTS Permission`; dynamic SQL (`EXECUTE IMMEDIATE`) kept dynamic and counted | `claude/ir7-plsql` | `Fnn-oracle-plsql` |
| W7.2 | Warehouse utilities and SQL in shell | `teradata` (`td`), `dbproc` (`dbp`), `shell` (`sh`) | Teradata TPT, FastLoad, MultiLoad and FastExport scripts; DB2 SQL PL, Netezza NZPLSQL and Sybase procedures; `.sh`, `.ksh`, `.bat`, `.cmd`, `.ps1` | Load and export scripts as `Program`s with source file and target table; procedures with behaviour; shell scripts as `Program`s: SQL in heredocs (`sqlplus`, `bteq`, `db2`, `nzsql`, `hive -e`, `psql`) through the SQL scanner, the tools they run (`sas`, `spark-submit`, `dsjob`, `pmcmd`, `dtexec`) as `RUNS`, and file transfers (`ftp`, `sftp`, `scp`, Connect:Direct) as dataset reads and writes with the host never copied | `claude/ir7-warehouse` | `Fnn-warehouse-shell` |
| W7.3 | SSIS and Informatica | `ssis` (`ssis`), `informatica` (`infa`) | `.dtsx`, `.conmgr`, `.params`, `.ispac`; PowerCenter XML exports (`POWERMART`), parameter files by name only | Packages and workflows as `Job`s; control-flow tasks and sessions as `Task`s with `DEPENDS_ON`; connection managers as `ConfigKey`s; each data flow or mapping as transform shapes (source, lookup, join, aggregate, router, target); embedded SQL through the scanner; script tasks counted, not modelled | `claude/ir7-etl-ms-infa` | `Fnn-ssis-package`, `Fnn-informatica-export` |
| W7.4 | DataStage, Talend and Ab Initio | `datastage` (`ds`), `talend` (`tal`), `abinitio` (`abi`) | `.dsx`, `.isx`; Talend `.item` and `.properties`; Ab Initio exports under `.rkp/inputs/abinitio/`, `.dml`, `.xfr` | As W7.3: jobs, sequences, stages as transform shapes, datasets and tables. Ab Initio graphs are binary: only `.dml` record formats (as `DataSchema`) and exported graph text are read, the rest is a named gap | `claude/ir7-etl-ibm-talend` | `Fnn-datastage-job`, `Fnn-talend-job` |
| W7.5 | Hadoop | `hadoop` (`hdp`) | `.hql`, `.q`, `.pig`, Oozie `workflow.xml` and `coordinator.xml`, Sqoop commands in Oozie actions and option files, Impala scripts | Hive and Impala scripts as `Program`s with lineage and transform shapes (dialect `hive`); Pig relations as transform shapes; Oozie workflows as `Job`s with action `Task`s and their transitions; coordinators as schedules; Sqoop imports and exports as dataset moves between a `Table` and a path | `claude/ir7-hadoop` | `Fnn-hadoop-oozie` |
| W7.6 | Batch schedulers | `scheduler` (`sched`) | Under `.rkp/inputs/scheduler/`: Control-M XML (`DEFTABLE`), AutoSys JIL, TWS/OPC composer text, CA-7 job listings, crontab, Windows Task Scheduler XML | Each definition as a `Job` with `attributes.schedule` and calendar name; in-conditions, out-conditions and `condition:` clauses as `DEPENDS_ON`; the JCL member, script or program it starts as `RUNS`, referred to without declaring it so it links only when the repository defines it | `claude/ir7-scheduler` | `Fnn-scheduler-exports` |

Notes for wave W7:

- W7.1 does not change `sql_lineage.py`; it uses the `oracle` dialect added by the foundation and reads SQL inside PL/SQL itself.
- The ETL packs (W7.3, W7.4) emit `transform` observations directly, with `E1` evidence, since their sources are declarative.
- W7.2's `shell` pack and W7.6 both name programs they do not declare. Neither resolves a name that the repository does not define.

## Wave W8: mainframe and midrange

| | Workstream | Packs (prefix) | Reads | Produces | Branch | Fixture |
|---|---|---|---|---|---|---|
| W8.1 | COBOL | `cobol` (`cbl`), supersedes `legacy.cobol` | `.cbl`, `.cob`, `.cobol`, `.cpy` | A real parser: divisions, sections, paragraphs, `PERFORM` graph; behaviour per paragraph; copybook layouts as `DataSchema` with `Field`s (`PIC`, `COMP-3`, `OCCURS`, `REDEFINES` as attributes); 88-levels as `CodeList`; `SELECT ... ASSIGN` and `FD` as dataset reads and writes by DD name; `EXEC SQL` as `query` nodes with host variables; `EXEC CICS` commands; a `CALL` through a variable resolved only when the variable has exactly one literal value in the file, otherwise dynamic | `claude/ir8-cobol` | `Fnn-cobol-batch-online` |
| W8.2 | z/OS job and resource definitions | `zos` (`zos`), supersedes `legacy.jcl` | `.jcl`, `.prc`, `.proc`; IDCAMS and DFSORT control cards in-stream or as members; CICS CSD exports (`DFHCSDUP` `DEFINE` statements), DB2 DDL, DCLGEN and `BIND` cards under `.rkp/inputs/zos/` | Everything `legacy.jcl` did; PROC overrides and symbolic parameters; `DEFINE CLUSTER` as `Dataset` with key and record length; DFSORT `SORT`, `INCLUDE`, `OUTREC`, `SUM` as transform shapes; CSD transactions as `Endpoint`s that a `Program` EXPOSES, and CSD files as the dataset a CICS file name stands for; DB2 plans and packages linking a program to its tables | `claude/ir8-zos` | `Fnn-zos-definitions` |
| W8.3 | Mainframe screens and IMS | `bms` (`bms`), `ims` (`ims`) | BMS map source (`DFHMSD`, `DFHMDI`, `DFHMDF`), IMS MFS, IMS `DBD` and `PSB` source | Maps as `Screen`, `Form` and `UIControl` (fields named by their labels, position and length as attributes); IMS segments as `Table`s (lossy) with their hierarchy; PCBs as what a program may read and write; IMS transactions as `Endpoint`s | `claude/ir8-screens-ims` | `Fnn-bms-ims` |
| W8.4 | IBM i | `ibmi` (`ibmi`) | RPG III and IV, fixed and free (`.rpg`, `.rpgle`, `.sqlrpgle`), CL (`.clp`, `.clle`), DDS (`.pf`, `.lf`, `.dspf`, `.prtf`) | Programs with behaviour per subroutine and procedure; physical files as `Table` and `Field`s, logical files as `DataSchema`; display files as `Screen`, `Form`, `UIControl`; printer files as `Report`; F-specs and embedded SQL as reads and writes; `CALL`, `CALLP`, `SBMJOB` and `OVRDBF` | `claude/ir8-ibmi` | `Fnn-ibmi-rpg` |
| W8.5 | Other mainframe languages | `pli` (`pli`), `easytrieve` (`ezt`), `rexx` (`rexx`), `hlasm` (`asm`) | `.pli`, `.pl1`, `.ezt`, `.rexx`, `.rex`, `.clist`, `.asm`, `.hlasm`, `.mac` | PL/I and Easytrieve programs with behaviour, `%INCLUDE`, `CALL`, files and embedded SQL; Easytrieve report sections as `Report`; REXX and CLIST as `Program`s with the commands and programs they run; assembler at construct level only: `CSECT`, `CALL`, `LINK`, macros used, counted and not lowered | `claude/ir8-zlang` | `Fnn-pli-easytrieve` |
| W8.6 | Natural, Adabas and IDMS | `natural` (`nat`), `idms` (`idms`) | Natural source exports (`.NSP`, `.NSN`, `.NSS`, `.NSM`, `.NSL`), Adabas DDMs (`.NSD`), IDMS schema and subschema source | Natural programs, subprograms and maps (`Screen`); DDMs as `Table` and `Field`s, with periodic groups and multiple-value fields as attributes; `READ`, `FIND`, `STORE`, `UPDATE` as reads and writes; IDMS records and sets as `Table`s (lossy) with set membership | `claude/ir8-natural-idms` | `Fnn-natural-adabas` |

Notes for wave W8:

- F14 must keep passing unchanged for W8.1 and W8.2 (rule 3 of section 2).
- DL/I calls (`CBLTDLI`), MQ calls and BMS `SEND MAP` appear in COBOL: W8.1 lists them as constructs and emits the name used; W8.3 declares the segment or map. The two link by name.
- COBOL on IBM i is parsed by `cobol`; `ibmi` owns only the files around it.

## Wave W9: application platforms

| | Workstream | Packs (prefix) | Reads | Produces | Branch | Fixture |
|---|---|---|---|---|---|---|
| W9.1 | Java EE | `javaee` (`jee`) | `struts-config.xml`, `struts.xml`, `ejb-jar.xml`, `faces-config.xml` and `.xhtml`, Spring bean XML, `persistence.xml`, Hibernate `.hbm.xml`, MyBatis and iBATIS mappers, `weblogic*.xml`, `ibm-*.xml`, `jboss-*.xml`, Ant `build.xml` | Struts actions as `Endpoint`s and their forwards as navigation; session, entity and message-driven beans (the last `SUBSCRIBES_TO` its destination); JSF screens and navigation rules; mapped tables; mapper SQL through the scanner; JNDI names and resource references as `ConfigKey`s; Ant targets as `BuildTarget`s; server-specific descriptors counted as constructs, which is the lock-in measure | `claude/ir9-javaee` | `Fnn-javaee-struts-ejb` |
| W9.2 | Legacy Microsoft | `vb6` (`vb6`), `classicasp` (`asp`), `dotnetfx` (`fx`) | `.vbp`, `.frm`, `.bas`, `.cls`, `.ctl`; `.asp`, `.asa`, `.inc`; `.svc`, `.asmx`, `web.config` and `app.config`, `packages.config`, pre-SDK project files, `.vb` | VB6 forms as `Screen`s with controls and event handlers with behaviour, ADO SQL, COM references; ASP pages as `Screen` and `Endpoint` with includes, ADO SQL and `Session` and `Application` scopes; WCF service contracts and bindings and ASMX methods as `Interface` and `Endpoint`s; connection string and app setting names; VB.NET controllers and data access as `source_dotnet` reads C#; COM interop, P/Invoke and .NET Remoting as constructs | `claude/ir9-microsoft` | `Fnn-vb6-classic-asp`, `Fnn-dotnet-framework` |
| W9.3 | Integration middleware and SOAP | `soap` (`soap`), `mule` (`mule`), `tibco` (`bw`), `iib` (`iib`), `bpel` (`bpel`), `biztalk` (`bts`) | `.wsdl`, `.xsd`; Mule XML, RAML, `.dwl`; TIBCO BW `.process` and `.bwp`; `.msgflow`, `.subflow`, `.esql`; `composite.xml`, `.bpel`; `.odx`, `.btm`, binding files; Camel XML routes | WSDL port types as `Interface` with `contract_operation`s, and XSD types as `DataSchema`; each flow, process or orchestration as a `Program` with a `flow` behaviour unit; its listeners as `Endpoint`s or `SUBSCRIBES_TO`, its outbound calls as `CALLS` or `PUBLISHES_TO`; maps (DataWeave, ESQL, BizTalk maps) as transform shapes where field-to-field, counted otherwise | `claude/ir9-integration` | `Fnn-soap-esb` |
| W9.4 | PowerBuilder and Oracle Forms | `powerbuilder` (`pb`), `oracleforms` (`ofm`) | PowerBuilder source exports (`.srw`, `.srd`, `.sru`, `.srm`, `.srf`); Forms and Reports XML (`frmf2xml`, `rwconverter`) under `.rkp/inputs/oracleforms/` | Windows and forms as `Screen`, `Form`, `UIControl`; DataWindows and blocks bound to tables (`BINDS_TO` a `Field`); event scripts and triggers with behaviour (Forms triggers lowered by the `plsql` pack's parser); menus as navigation; Oracle Reports as `Report` with their queries | `claude/ir9-clientserver` | `Fnn-powerbuilder`, `Fnn-oracle-forms` |
| W9.5 | Perl and PHP | `perl` (`perl`), `php` (`php`) | `.pl`, `.pm`, `.cgi`; `.php`, `.phtml`, `.inc` | Perl scripts and modules as `Program`s, CGI scripts as `Endpoint`s, DBI SQL through the scanner, `system` and backtick commands as `RUNS`; PHP pages as `Screen` and `Endpoint` with forms and controls by the naming rule of section 1, `include` and `require`, `mysql_*`, `mysqli` and PDO SQL, `$_SESSION` scope; Laravel and Symfony routes when literal | `claude/ir9-perl-php` | `Fnn-perl-php` |
| W9.6 | Legacy front ends | extends `web`; new `xaml` (`xaml`), `flex` (`flex`) | AngularJS 1.x templates and modules, jQuery, Backbone and Knockout pages; `.xaml` (WPF, Silverlight); `.mxml`, `.as` | AngularJS routes (`$routeProvider`, ui-router states) as `Screen`s, `ng-model` and `data-bind` as bindings, `$http` and `$.ajax` calls with method (8.3); XAML and MXML views as `Screen`, `Form`, `UIControl` with bindings and code-behind handlers | `claude/ir9-frontends` | `Fnn-angularjs-jquery`, `Fnn-xaml-flex` |

Notes for wave W9:

- `web.xml` stays with the `jsp` pack and `.aspx` with `aspnet` (wave 2, W6). W9.1 and W9.2 read only the files listed above.
- W9.6 also owns `repo_intel/platforms/web/**`; it starts after wave 2's W5 is integrated.
- W9.4 depends on W7.1 for the PL/SQL parser and imports it inside functions (rule 1 of section 2).

## Wave W10: packaged platforms, reporting and findings

| | Workstream | Packs or files | Reads | Produces | Branch | Fixture |
|---|---|---|---|---|---|---|
| W10.1 | Reporting and BI | `reports` (`rpt`) | SSRS `.rdl`, `.rds`, `.rsd`; SSAS and Power BI tabular `.bim` and TMDL; Cognos report specification XML; Crystal and BusinessObjects exports under `.rkp/inputs/reports/` | Each report as a `Report` with its parameters as a `Form`, its dataset queries through the scanner (`READS_FROM` tables), shared data sources as `ConfigKey`s, subreports and drill-throughs as navigation; semantic-model tables, measures and relationships | `claude/ir10-reports` | `Fnn-ssrs-cognos` |
| W10.2 | SAP and Salesforce | `abap` (`abap`), `salesforce` (`sf`) | abapGit repositories (`.abap` with their `.xml`); Salesforce DX source (`.cls`, `.trigger`, `.page`, `.component`, Aura, LWC, `*.object-meta.xml`, `*.flow-meta.xml`, permission sets) | ABAP reports, classes and function modules with behaviour, DDIC tables and CDS views, transaction codes as `Endpoint`s, `SELECT`s as reads, custom code (`Z*`, `Y*`) flagged; Apex classes and triggers with behaviour and SOQL, objects and fields as `Table` and `Field`, pages and components as `Screen`s, flows as `Workflow`, profiles and permission sets as `Role GRANTS Permission` | `claude/ir10-sap-salesforce` | `Fnn-abap`, `Fnn-salesforce` |
| W10.3 | Long tail | `delphi` (`dph`), `progress` (`abl`), `informix4gl` (`i4gl`), `proc` (`proc`) | `.pas`, `.dfm`, `.dpr`; `.p`, `.w`, `.i`, `.cls` (ABL); `.4gl`, `.per`; `.pc` and C or C++ with `EXEC SQL` | Forms as `Screen`s, units and procedures as `Program`s, embedded and dataset SQL as reads and writes. Structure and constructs first; behaviour only where it validates | `claude/ir10-longtail` | `Fnn-long-tail` |
| W10.4 | Migration findings | `repo_intel/compile/patterns.py` (migration entries only), `architecture_patterns.json` (new family), `repo_intel/compile/readiness.py` (new), `repo_intel/compile/runtime_eol.json` (new), `tests/test_readiness.py` | Compiled knowledge only; no source files | See below | `claude/ir10-findings` | `Fnn-migration-findings` |
| W10.5 | Inventory command and migration plane | `repo_intel/cli.py` (`inventory` only), `repo-intelligence/explorer/**`, `tests/test_cli_inventory.py` | Bundles and solutions | `rkp inventory`: per platform, the programs, jobs, screens, reports and tables found, lines read, construct coverage, and gaps that need an export. An Explorer migration plane: the same, plus W10.4's findings and, for a solution, which legacy entities already have a modern counterpart | `claude/ir10-inventory` | none |

### Migration findings (W10.4)

Each finding is derived from compiled entities, relations and construct counts, cites its evidence, and is an antipattern or trait in the existing taxonomy. None needs a model.

| Group | Finding | Derived from |
|---|---|---|
| Integration style | Shared database | One `Table` written by programs of different platforms or solution members |
| | Logic in the database | Share of `BusinessRule` candidates and behaviour units owned by procedures and triggers |
| | File-drop integration | A `Dataset` written by one job and read by another with no call between them; transfers from the `shell` pack |
| | Remote procedure styles | SOAP, RMI, DCOM, .NET Remoting and CORBA constructs |
| Blockers | End-of-life runtime | Manifest and project versions against `runtime_eol.json` (Java, .NET Framework, Python 2, Struts 1, AngularJS and others, each with its source and date) |
| | Platform coupling | COM interop, P/Invoke, JNI, registry access, server-specific descriptors |
| | Local state | Writes to literal local paths, in-process session scopes |
| | Hard-coded hosts | Host literals counted, never copied |
| Data hazards | Packed and overlaid data | `COMP-3`, `REDEFINES`, `OCCURS DEPENDING ON` in layouts that a `Dataset` uses |
| | Dynamic SQL | Share of `query` nodes that are dynamic, per program |
| | Distributed transactions | XA, two-phase commit, `SYNCPOINT` |
| Retirement | Unreached program | A `Program` that no job, endpoint, screen or program reaches; reported only when scheduler definitions (W7.6) are present, otherwise as unknown |
| | Unused layout, unread table | A copybook nothing includes; a table written and never read |
| In progress | Counterpart exists | A legacy and a modern program write the same `Dataset` or expose the same `Endpoint` |
| | Change data capture, strangler routing | Debezium and GoldenGate configuration; a gateway route that splits one path between two members |

Coverage domain `migration_readiness`: platforms with a pack over platforms inventoried by file type.

## Not planned

| Platform | Reason |
|---|---|
| Fortran, Ada, MUMPS, Tandem TAL, OpenVMS DCL | Narrow sectors; add on demand as inventory-only packs |
| PeopleSoft, Siebel, Oracle E-Business Suite, Dynamics AX, Pega | Customisation lives in the product's database, not in files; needs a vendor export format settled first |
| Lotus Notes and Domino, FoxPro, Microsoft Access | Binary containers; needs an owner-run export |
| Objective-C, Xamarin | Modernization of mobile code is ordinary source work; belongs with the language extractors |
| MicroStrategy, Tableau, Qlik | Add to `reports` after W10.1 if asked |

## Rules for every workstream

- Sections 5 to 7 of the contract: a pack test file, one golden fixture with `since: "M2"`, the full test suite and `golden_check` passing, the review checklist, and a closing report with construct coverage and requests for the foundation.
- Fixture numbers (`Fnn`) are allocated when a wave opens, following the last number waves W3 to W6 take, and must stay contiguous.
- An honest construct list (section 2): list what a real application uses, model what can be read with certainty, count the rest.
- Raise a request for the foundation, not a change to a shared file, when a core kind or relation is missing.
