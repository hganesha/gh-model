"""The Informatica pack's vocabulary: platform terms, the constructs it looks for, and how
transformation and task types are recognised."""

from __future__ import annotations

from repo_intel.compile.ontology import EntityKind as K
from repo_intel.platforms import Construct, Term

PLATFORM = "informatica"
FRAMEWORK = "informatica"

TERMS = (
    Term("infa:Workflow", K.JOB, "lossy", "a PowerCenter workflow; named <folder>/<workflow>"),
    Term("infa:Worklet", K.JOB, "lossy", "a reusable worklet, run by a worklet task; named <folder>/<worklet>"),
    Term("infa:Session", K.TASK, "lossy", "a session task instance of a workflow or worklet; it runs one mapping"),
    Term("infa:Task", K.TASK, "lossy", "any other task instance of a workflow or worklet (command, email, decision, timer, ...)"),
    Term("infa:Mapping", K.PROGRAM, "lossy", "a mapping: its transformations as transform shapes, row-level logic as a flow unit; named <folder>/<mapping>"),
    Term("infa:Connection", K.CONFIG_KEY, "lossy", "a relational or application connection by name (connection:<name>); its connect string is never read"),
    Term("infa:Parameter", K.CONFIG_KEY, "lossy", "a mapping parameter ($$NAME) or a session parameter ($NAME) by name; its value is never read"),
    Term("infa:FlatFile", K.DATA_SCHEMA, "lossy", "a flat file, XML or VSAM source or target definition, a record layout; named informatica:<folder>/<name>"),
    Term("infa:FlatFileField", K.FIELD, "lossy", "a field of a flat file definition; table informatica:<folder>/<name>"),
)

# DATABASETYPE values of a definition that is not a database table.
NON_RELATIONAL = {"flat file", "vsam", "xml", "cobol", "imsdb", "application", "salesforce", "sap r/3", "sap bw", "web service", "jms", "mq series"}
# DATABASETYPE of a source -> the SQL dialect its Source Qualifier SQL is read as.
DIALECTS = {
    "oracle": "oracle", "microsoft sql server": "tsql", "sybase": "sybase", "teradata": "teradata",
    "db2": "db2", "netezza": "netezza", "odbc": None, "informix": None,
}

# TRANSFORMATION TYPE (lower case) -> (role, construct).
TRANSFORMATIONS = {
    "source qualifier": ("source_qualifier", "infa:transformation.source_qualifier"),
    "application source qualifier": ("source_qualifier", "infa:transformation.source_qualifier"),
    "expression": ("expression", "infa:transformation.expression"),
    "filter": ("filter", "infa:transformation.filter"),
    "router": ("router", "infa:transformation.router"),
    "joiner": ("joiner", "infa:transformation.joiner"),
    "lookup procedure": ("lookup", "infa:transformation.lookup"),
    "aggregator": ("aggregator", "infa:transformation.aggregator"),
    "sorter": ("sorter", "infa:transformation.sorter"),
    "union transformation": ("union", "infa:transformation.union"),
    "rank": ("rank", "infa:transformation.rank"),
    "update strategy": ("update_strategy", "infa:transformation.update_strategy"),
    "sequence": ("sequence", "infa:transformation.sequence_generator"),
    "sequence generator": ("sequence", "infa:transformation.sequence_generator"),
    "normalizer": ("other", "infa:transformation.normalizer"),
    "stored procedure": ("other", "infa:transformation.stored_procedure"),
    "java": ("other", "infa:transformation.java"),
    "custom transformation": ("other", "infa:transformation.custom"),
    "sql": ("other", "infa:transformation.sql"),
    "transaction control": ("other", "infa:transformation.transaction_control"),
    "xml parser": ("other", "infa:transformation.xml"),
    "xml generator": ("other", "infa:transformation.xml"),
    "xml source qualifier": ("source_qualifier", "infa:transformation.source_qualifier"),
    "mapplet": ("mapplet", "infa:mapplet_instance"),
    "input transformation": ("pass", "infa:transformation.mapplet_port"),
    "output transformation": ("pass", "infa:transformation.mapplet_port"),
}

# Workflow task types (lower case) -> (task type, construct).
TASK_TYPES = {
    "start": ("start", "infa:task.start"),
    "session": ("session", "infa:session"),
    "command": ("command", "infa:task.command"),
    "email": ("email", "infa:task.email"),
    "decision": ("decision", "infa:task.decision"),
    "timer": ("timer", "infa:task.timer"),
    "event wait": ("event_wait", "infa:task.event_wait"),
    "event raise": ("event_raise", "infa:task.event_raise"),
    "assignment": ("assignment", "infa:task.assignment"),
    "control": ("control", "infa:task.control"),
    "worklet": ("worklet", "infa:task.worklet"),
}

_CONSTRUCTS = (
    # Repository objects
    ("export", "attribute:repository", "a PowerCenter XML export (POWERMART root) and its repository"),
    ("folder", "attribute:folder", "a repository folder; objects are named <folder>/<name>"),
    ("source.relational", "entity:Table", "a relational source definition"),
    ("source.flat_file", "entity:DataSchema", "a flat file, XML or VSAM source definition"),
    ("target.relational", "entity:Table", "a relational target definition"),
    ("target.flat_file", "entity:DataSchema", "a flat file or XML target definition"),
    ("shortcut", "unmapped", "a shortcut to an object of another folder (the object itself is not in this export)"),
    ("mapping", "entity:Program", "a mapping"),
    ("mapplet", "unmapped", "a mapplet definition (reusable transformation logic; not lowered)"),
    ("mapplet_instance", "unmapped", "a mapplet used inside a mapping"),
    ("mapping_parameter", "entity:ConfigKey", "a mapping parameter ($$NAME)"),
    ("mapping_variable", "attribute:variables", "a mapping variable (a value kept between runs; its value is never read)"),
    ("connector", "attribute:transforms", "a connector between the ports of two instances of a mapping"),
    ("reusable_transformation", "attribute:transforms", "a reusable transformation defined in the folder and used in a mapping"),
    # Transformations
    ("transformation.source_qualifier", "attribute:transforms", "a Source Qualifier: the read of its sources, with a source filter, sorted ports and distinct"),
    ("transformation.sql_override", "behavior:query", "a Source Qualifier or Lookup whose SQL query is overridden, read by the SQL scanner"),
    ("transformation.pre_post_sql", "relation:WRITES_TO", "Pre SQL and Post SQL of a Source Qualifier or target"),
    ("transformation.expression", "attribute:transforms", "an Expression transformation (output and variable ports)"),
    ("transformation.filter", "attribute:transforms", "a Filter transformation"),
    ("transformation.router", "attribute:transforms", "a Router transformation: one filter per group and the default group"),
    ("transformation.joiner", "attribute:transforms", "a Joiner transformation (normal, master outer, detail outer, full outer)"),
    ("transformation.lookup", "attribute:transforms", "a connected Lookup transformation, a left join to the lookup table"),
    ("transformation.lookup_unconnected", "relation:READS_FROM", "an unconnected Lookup called from an expression (:LKP); its table is read"),
    ("transformation.aggregator", "attribute:transforms", "an Aggregator transformation (group-by ports and aggregate functions)"),
    ("transformation.sorter", "attribute:transforms", "a Sorter transformation (sort keys and distinct)"),
    ("transformation.union", "attribute:transforms", "a Union transformation"),
    ("transformation.rank", "attribute:transforms", "a Rank transformation (top or bottom rows per group)"),
    ("transformation.update_strategy", "attribute:transforms", "an Update Strategy transformation (DD_INSERT, DD_UPDATE, DD_DELETE, DD_REJECT)"),
    ("transformation.sequence_generator", "unmapped", "a Sequence Generator (NEXTVAL and CURRVAL ports)"),
    ("transformation.normalizer", "unmapped", "a Normalizer transformation"),
    ("transformation.stored_procedure", "unmapped", "a Stored Procedure transformation"),
    ("transformation.java", "unmapped", "a Java transformation (code; counted, not read)"),
    ("transformation.custom", "unmapped", "a Custom transformation"),
    ("transformation.sql", "unmapped", "a SQL transformation (queries built at run time)"),
    ("transformation.transaction_control", "unmapped", "a Transaction Control transformation"),
    ("transformation.xml", "unmapped", "an XML Parser or XML Generator transformation"),
    ("transformation.mapplet_port", "unmapped", "the Input or Output transformation of a mapplet"),
    ("transformation.other", "unmapped", "a transformation type the reader does not know"),
    ("expression_unread", "unmapped", "a transformation-language expression the reader could not read"),
    ("target_instance", "relation:WRITES_TO", "a target instance of a mapping"),
    ("source_instance", "relation:READS_FROM", "a source instance of a mapping"),
    ("instance_unresolved", "unmapped", "an instance whose definition is not in the export (a shortcut or a missing object)"),
    # Sessions and workflows
    ("session", "entity:Task", "a session task instance, running one mapping"),
    ("session_override", "unmapped", "a session-level override of a transformation attribute (SQL query, table name, filter)"),
    ("session_config", "unmapped", "a session configuration object"),
    ("connection_reference", "relation:REFERENCES_CONFIG", "a connection a session's reader, writer or lookup uses, by name"),
    ("connection_variable", "relation:REFERENCES_CONFIG", "a connection named by a session parameter ($DBConnection_...): resolved only by a parameter file"),
    ("connection_builtin", "unmapped", "the $Source or $Target connection placeholders"),
    ("workflow", "entity:Job", "a workflow"),
    ("worklet", "entity:Job", "a worklet"),
    ("workflow_link", "relation:DEPENDS_ON", "a link between two task instances of a workflow or worklet, with its condition"),
    ("workflow_variable", "attribute:variables", "a user-defined workflow variable"),
    ("scheduler", "attribute:schedule", "the scheduler of a workflow"),
    ("task.start", "attribute:entry", "the Start task of a workflow or worklet"),
    ("task.command", "unmapped", "a Command task (shell commands; not read)"),
    ("task.email", "unmapped", "an Email task"),
    ("task.decision", "entity:Task", "a Decision task (its condition is kept as an attribute-free Task)"),
    ("task.timer", "unmapped", "a Timer task"),
    ("task.event_wait", "unmapped", "an Event-Wait task (a file watch or a user event)"),
    ("task.event_raise", "unmapped", "an Event-Raise task"),
    ("task.assignment", "unmapped", "an Assignment task (sets workflow variables)"),
    ("task.control", "unmapped", "a Control task (stops or aborts the workflow)"),
    ("task.worklet", "relation:RUNS", "a worklet task instance, running the worklet"),
    ("task.other", "unmapped", "a task type the reader does not know"),
    ("parameter_file_name", "attribute:parameter_file", "the parameter file a workflow or session names (its base name only)"),
    # Parameter files
    ("parameter_file", "attribute:parameters", "a parameter file (.par, .prm, .parm)"),
    ("parameter_section", "relation:REFERENCES_CONFIG", "a [folder.WF:workflow] or [folder.WF:workflow.ST:session] section of a parameter file"),
    ("parameter_entry", "entity:ConfigKey", "a parameter set in a parameter file (its value is never read)"),
    ("parameter_builtin", "unmapped", "a built-in session parameter ($PMSessionLogFile, $PMRootDir, ...) set in a parameter file"),
)

CONSTRUCTS = tuple(Construct(f"infa:{cid}", description, modelled) for cid, modelled, description in _CONSTRUCTS)


def transformation(type_name: str | None) -> tuple[str, str]:
    return TRANSFORMATIONS.get((type_name or "").strip().lower(), ("other", "infa:transformation.other"))


def task_type(type_name: str | None) -> tuple[str, str]:
    return TASK_TYPES.get((type_name or "").strip().lower(), ("other", "infa:task.other"))
