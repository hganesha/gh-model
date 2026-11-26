"""The Hadoop pack's vocabulary: platform terms and the constructs it looks for.

Constructs are listed as a real Hadoop estate uses them, modelled or not: an unmodelled
construct that is counted shows what the pack does not yet read.
"""

from __future__ import annotations

from repo_intel.compile.ontology import EntityKind as K
from repo_intel.platforms import Construct, Term


def _t(name: str, core: K, fidelity: str, description: str) -> Term:
    return Term(f"hdp:{name}", core, fidelity, description)  # type: ignore[arg-type]


def _c(name: str, modelled_as: str, description: str) -> Construct:
    return Construct(f"hdp:{name}", description, modelled_as)


TERMS = (
    _t("HiveScript", K.PROGRAM, "lossy", "a Hive or Impala script (.hql, or .q or .sql read as HiveQL); platform hive, named by its path"),
    _t("PigScript", K.PROGRAM, "lossy", "a Pig Latin script (.pig); platform pig, named by its path"),
    _t("SqoopJob", K.PROGRAM, "lossy", "a Sqoop options file that states an import or export; platform sqoop, named by its path"),
    _t("OozieWorkflow", K.JOB, "lossy", "an Oozie workflow application (root workflow-app); framework oozie, named by its path"),
    _t("OozieAction", K.TASK, "lossy", "an action node of an Oozie workflow; its type (hive, pig, sqoop, shell, ...) in attributes"),
    _t("OozieCoordinator", K.JOB, "lossy", "an Oozie coordinator application (root coordinator-app): the schedule that starts a workflow"),
    _t("OozieBundle", K.JOB, "lossy", "an Oozie bundle application (root bundle-app): the coordinators it starts"),
)

CONSTRUCTS = (
    # Hive and Impala scripts.
    _c("hive_script", "entity:Program", "a Hive or Impala script"),
    _c("hive_insert_overwrite", "relation:WRITES_TO", "INSERT OVERWRITE TABLE ... SELECT"),
    _c("hive_insert_into", "relation:WRITES_TO", "INSERT INTO [TABLE] ... SELECT or VALUES"),
    _c("hive_multi_insert", "relation:WRITES_TO", "FROM source INSERT ... INSERT ...: several targets from one scan"),
    _c("hive_ctas", "relation:WRITES_TO", "CREATE TABLE ... AS SELECT"),
    _c("hive_select", "relation:READS_FROM", "a query that only reads"),
    _c("hive_create_table", "entity:Table", "CREATE TABLE with columns: the table and its Fields"),
    _c("hive_external_table", "relation:DERIVED_FROM", "CREATE EXTERNAL TABLE ... LOCATION: the table over a path"),
    _c("hive_partitioned_by", "attribute:partitioned_by", "PARTITIONED BY columns of a table"),
    _c("hive_create_view", "entity:DataSchema", "CREATE VIEW ... AS SELECT: a view and the tables it reads"),
    _c("hive_load_data", "relation:WRITES_TO", "LOAD DATA INPATH ... INTO TABLE: files moved into a table"),
    _c("hive_insert_directory", "relation:WRITES_TO", "INSERT OVERWRITE [LOCAL] DIRECTORY ... SELECT: a query written to a path"),
    _c("hive_merge", "relation:WRITES_TO", "MERGE INTO (ACID tables)"),
    _c("hive_update_delete", "relation:WRITES_TO", "UPDATE or DELETE (ACID tables), or TRUNCATE TABLE"),
    _c("hive_dynamic_partition", "attribute:dynamic_partitions", "an INSERT whose PARTITION clause names a column without a value"),
    _c("hive_variable", "attribute:variables", "a substitution (${hivevar:x}, ${hiveconf:x}, ${env:x}, ${x}); kept as a {placeholder}, never resolved"),
    _c("hive_set", "attribute:settings", "SET name=value: the name is kept, never the value"),
    _c("hive_use", "attribute:databases", "USE database: qualifies the names that follow"),
    _c("hive_add_resource", "unmapped", "ADD JAR, ADD FILE, ADD ARCHIVE"),
    _c("hive_temporary_function", "unmapped", "CREATE [TEMPORARY] FUNCTION ... AS class"),
    _c("hive_source", "unmapped", "source <file> in the Hive CLI: another script run in place"),
    _c("hive_shell_command", "unmapped", "!command or dfs command in the Hive CLI"),
    _c("hive_transform_script", "unmapped", "SELECT TRANSFORM (...) USING 'script' or MAP/REDUCE"),
    _c("hive_lateral_view", "unmapped", "LATERAL VIEW explode(...)"),
    _c("hive_msck_repair", "unmapped", "MSCK REPAIR TABLE"),
    _c("hive_alter_table", "unmapped", "ALTER TABLE (partitions, properties, columns)"),
    _c("hive_analyze", "unmapped", "ANALYZE TABLE ... COMPUTE STATISTICS"),
    _c("hive_drop", "unmapped", "DROP TABLE, VIEW, DATABASE or PARTITION"),
    _c("hive_create_database", "unmapped", "CREATE DATABASE or SCHEMA"),
    _c("impala_compute_stats", "unmapped", "COMPUTE [INCREMENTAL] STATS (Impala)"),
    _c("impala_invalidate_metadata", "unmapped", "INVALIDATE METADATA (Impala)"),
    _c("impala_refresh", "unmapped", "REFRESH table (Impala)"),
    _c("hive_unparsed", "unmapped", "a statement the reader cannot read"),
    # Pig Latin.
    _c("pig_script", "entity:Program", "a Pig Latin script"),
    _c("pig_load", "relation:READS_FROM", "LOAD a path or an HCatalog table"),
    _c("pig_store", "relation:WRITES_TO", "STORE a relation into a path or an HCatalog table"),
    _c("pig_filter", "attribute:transforms", "FILTER ... BY"),
    _c("pig_foreach", "attribute:transforms", "FOREACH ... GENERATE"),
    _c("pig_nested_foreach", "unmapped", "FOREACH ... { nested block } GENERATE"),
    _c("pig_flatten", "unmapped", "FLATTEN in a GENERATE list"),
    _c("pig_group", "attribute:transforms", "GROUP ... BY or ALL"),
    _c("pig_cogroup", "unmapped", "COGROUP, or GROUP of several relations"),
    _c("pig_join", "attribute:transforms", "JOIN ... BY (inner and outer)"),
    _c("pig_join_strategy", "unmapped", "JOIN ... USING 'replicated', 'skewed' or 'merge'"),
    _c("pig_cross", "attribute:transforms", "CROSS"),
    _c("pig_distinct", "attribute:transforms", "DISTINCT"),
    _c("pig_order", "attribute:transforms", "ORDER ... BY"),
    _c("pig_limit", "attribute:transforms", "LIMIT"),
    _c("pig_union", "attribute:transforms", "UNION [ONSCHEMA]"),
    _c("pig_split", "attribute:transforms", "SPLIT ... INTO ... IF"),
    _c("pig_sample", "unmapped", "SAMPLE"),
    _c("pig_rank", "unmapped", "RANK"),
    _c("pig_cube", "unmapped", "CUBE or ROLLUP"),
    _c("pig_stream", "unmapped", "STREAM ... THROUGH a command"),
    _c("pig_mapreduce", "unmapped", "MAPREDUCE: a native MapReduce job"),
    _c("pig_assert", "unmapped", "ASSERT ... BY"),
    _c("pig_register", "unmapped", "REGISTER a jar or script"),
    _c("pig_define", "unmapped", "DEFINE an alias for a function or command"),
    _c("pig_macro", "unmapped", "DEFINE ... RETURNS { }: a macro"),
    _c("pig_import", "unmapped", "IMPORT a macro file"),
    _c("pig_parameter", "attribute:parameters", "%declare, %default or $name substitution; names kept, values never"),
    _c("pig_set", "attribute:settings", "SET key value: the key is kept, never the value"),
    _c("pig_diagnostic", "unmapped", "DUMP, DESCRIBE, EXPLAIN or ILLUSTRATE"),
    _c("pig_fs_command", "unmapped", "fs, sh, rmf, rm, mkdir, cp, mv, copyFromLocal, copyToLocal"),
    _c("pig_unknown_statement", "unmapped", "a statement the reader does not know"),
    # Oozie.
    _c("oozie_workflow", "entity:Job", "a workflow application"),
    _c("oozie_coordinator", "entity:Job", "a coordinator application"),
    _c("oozie_bundle", "entity:Job", "a bundle application"),
    _c("oozie_action_hive", "entity:Task", "a hive or hive2 action; RUNS the script"),
    _c("oozie_action_pig", "entity:Task", "a pig action; RUNS the script"),
    _c("oozie_action_sqoop", "entity:Task", "a sqoop action: a dataset move"),
    _c("oozie_action_shell", "entity:Task", "a shell action; RUNS the script when its path is in the application"),
    _c("oozie_action_spark", "entity:Task", "a spark action; RUNS a PySpark file in the application"),
    _c("oozie_action_java", "entity:Task", "a java or map-reduce action; the class names in attributes"),
    _c("oozie_action_fs", "entity:Task", "an fs action (delete, mkdir, move, chmod)"),
    _c("oozie_action_distcp", "entity:Task", "a distcp action: copies paths"),
    _c("oozie_action_email", "entity:Task", "an email action; recipients are never read"),
    _c("oozie_action_ssh", "entity:Task", "an ssh action; the host is never read"),
    _c("oozie_action_subworkflow", "entity:Task", "a sub-workflow action; RUNS the workflow when its path is literal"),
    _c("oozie_action_other", "entity:Task", "a custom or unknown action type"),
    _c("oozie_transition", "relation:DEPENDS_ON", "an ok or error transition between nodes"),
    _c("oozie_decision", "behavior:if", "a decision node: switch with cases and a default"),
    _c("oozie_fork", "behavior:platform", "a fork node: paths that run in parallel"),
    _c("oozie_join", "behavior:platform", "a join node: waits for the paths of a fork"),
    _c("oozie_kill", "behavior:throw", "a kill node"),
    _c("oozie_end", "behavior:exit", "an end node"),
    _c("oozie_global", "unmapped", "global configuration of a workflow"),
    _c("oozie_credentials", "unmapped", "credentials (HCat, Hive2, HBase); never read"),
    _c("oozie_parameters", "attribute:parameters", "declared parameters; names kept, defaults never"),
    _c("oozie_prepare", "unmapped", "prepare steps of an action (delete, mkdir)"),
    _c("oozie_sla", "unmapped", "an SLA block"),
    _c("oozie_coord_dataset", "entity:Dataset", "a coordinator dataset (uri-template)"),
    _c("oozie_coord_input", "relation:READS_FROM", "a coordinator input event"),
    _c("oozie_coord_output", "relation:WRITES_TO", "a coordinator output event"),
    _c("oozie_coord_action", "relation:RUNS", "the workflow a coordinator starts"),
    _c("oozie_bundle_coordinator", "relation:RUNS", "a coordinator a bundle starts"),
    # Sqoop.
    _c("sqoop_import", "relation:WRITES_TO", "sqoop import: a table copied to a path or a Hive table"),
    _c("sqoop_export", "relation:WRITES_TO", "sqoop export: a path copied into a table"),
    _c("sqoop_free_form_query", "relation:READS_FROM", "sqoop import --query: the tables the query reads"),
    _c("sqoop_hive_import", "relation:WRITES_TO", "--hive-import or --hcatalog-table: the Hive table written or read"),
    _c("sqoop_incremental", "attribute:transforms", "--incremental append or lastmodified on a check column"),
    _c("sqoop_update_key", "attribute:transforms", "export --update-key: an update or upsert by key"),
    _c("sqoop_connect", "attribute:engine", "--connect: only the JDBC engine name is kept, never the connection string"),
    _c("sqoop_password", "unmapped", "--password, -P or --password-file: never read"),
    _c("sqoop_options_file", "unmapped", "--options-file: options in another file, not read here"),
    _c("sqoop_import_all_tables", "unmapped", "sqoop import-all-tables: the tables are named only at run time"),
    _c("sqoop_eval", "unmapped", "sqoop eval: SQL run in the source database"),
    _c("sqoop_merge", "unmapped", "sqoop merge"),
    _c("sqoop_job", "unmapped", "sqoop job --create: a saved job"),
    _c("sqoop_other_tool", "unmapped", "another sqoop tool (list-tables, codegen, create-hive-table, ...)"),
)

# The Oozie action types and the construct each is counted as.
ACTION_CONSTRUCTS = {
    "hive": "hdp:oozie_action_hive", "hive2": "hdp:oozie_action_hive", "pig": "hdp:oozie_action_pig", "sqoop": "hdp:oozie_action_sqoop",
    "shell": "hdp:oozie_action_shell", "spark": "hdp:oozie_action_spark", "java": "hdp:oozie_action_java", "map-reduce": "hdp:oozie_action_java",
    "fs": "hdp:oozie_action_fs", "distcp": "hdp:oozie_action_distcp", "email": "hdp:oozie_action_email", "ssh": "hdp:oozie_action_ssh",
    "sub-workflow": "hdp:oozie_action_subworkflow",
}
