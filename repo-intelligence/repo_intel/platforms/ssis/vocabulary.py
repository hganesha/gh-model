"""The SSIS pack's vocabulary: platform terms, the constructs it looks for, and how task
and data flow component types are recognised."""

from __future__ import annotations

from repo_intel.compile.ontology import EntityKind as K
from repo_intel.platforms import Construct, Term

PLATFORM = "ssis"
FRAMEWORK = "ssis"

TERMS = (
    Term("ssis:Package", K.JOB, "lossy", "an SSIS package (.dtsx, or a package inside an .ispac); named by its path, <archive>#<member> inside an archive"),
    Term("ssis:Task", K.TASK, "lossy", "a control-flow task or container of a package; named by the path of names from the package down, joined with '/'"),
    Term("ssis:DataFlow", K.PROGRAM, "lossy", "the pipeline of a Data Flow task: components as transform shapes, row-level logic as a flow unit; named <package>#<task path>"),
    Term("ssis:SqlTask", K.PROGRAM, "lossy", "the SQL statements of an Execute SQL task, as a sql_step unit; named <package>#<task path>"),
    Term("ssis:ConnectionManager", K.CONFIG_KEY, "lossy", "a connection manager by name (connection:<name>); its connection string is never read"),
    Term("ssis:Parameter", K.CONFIG_KEY, "lossy", "a project or package parameter by name; its value is never read"),
    Term("ssis:FlatFileLayout", K.DATA_SCHEMA, "lossy", "the columns of a flat file connection manager, a record layout; named ssis:<connection name>"),
    Term("ssis:FlatFileColumn", K.FIELD, "lossy", "a column of a flat file layout; table ssis:<connection name>"),
)

# Control-flow executables: a marker in the executable type (case-insensitive) -> (task type, construct).
# Types are matched by substring so the 2008 form (`Microsoft.SqlServer.Dts.Tasks.ExecuteSQLTask...`)
# and the 2012 form (`Microsoft.ExecuteSQLTask`) read the same.
TASK_TYPES = (
    ("executesqltask", "execute_sql", "ssis:task.execute_sql"),
    ("pipeline", "data_flow", "ssis:task.data_flow"),
    ("executepackagetask", "execute_package", "ssis:task.execute_package"),
    ("scripttask", "script", "ssis:task.script"),
    ("filesystemtask", "file_system", "ssis:task.file_system"),
    ("ftptask", "ftp", "ssis:task.ftp"),
    ("sendmailtask", "send_mail", "ssis:task.send_mail"),
    ("executeprocess", "execute_process", "ssis:task.execute_process"),
    ("expressiontask", "expression", "ssis:task.expression"),
    ("bulkinserttask", "bulk_insert", "ssis:task.bulk_insert"),
    ("webservicetask", "web_service", "ssis:task.web_service"),
    ("xmltask", "xml", "ssis:task.xml"),
    ("dataprofilingtask", "data_profiling", "ssis:task.data_profiling"),
    ("cdccontroltask", "cdc_control", "ssis:task.cdc_control"),
    ("stock:sequence", "sequence", "ssis:container.sequence"),
    ("stock:forloop", "for_loop", "ssis:container.for_loop"),
    ("stock:foreachloop", "foreach_loop", "ssis:container.foreach_loop"),
)
CONTAINERS = {"sequence", "for_loop", "foreach_loop"}

# Data flow components: componentClassID (lower case) -> (role, construct).
COMPONENTS = {
    "microsoft.oledbsource": ("source_db", "ssis:dataflow.source"),
    "microsoft.adonetsource": ("source_db", "ssis:dataflow.source"),
    "microsoft.managedsource": ("source_db", "ssis:dataflow.source"),
    "microsoft.odbcsource": ("source_db", "ssis:dataflow.source"),
    "microsoft.flatfilesource": ("source_file", "ssis:dataflow.source_flat_file"),
    "microsoft.excelsource": ("source_other", "ssis:dataflow.source_other"),
    "microsoft.rawsource": ("source_other", "ssis:dataflow.source_other"),
    "microsoft.xmlsourceadapter": ("source_other", "ssis:dataflow.source_other"),
    "microsoft.cdcsource": ("source_other", "ssis:dataflow.source_other"),
    "microsoft.oledbdestination": ("dest_db", "ssis:dataflow.destination"),
    "microsoft.adonetdestination": ("dest_db", "ssis:dataflow.destination"),
    "microsoft.odbcdestination": ("dest_db", "ssis:dataflow.destination"),
    "microsoft.sqlserverdestination": ("dest_db", "ssis:dataflow.destination"),
    "microsoft.flatfiledestination": ("dest_file", "ssis:dataflow.destination_flat_file"),
    "microsoft.exceldestination": ("dest_other", "ssis:dataflow.destination_other"),
    "microsoft.rawdestination": ("dest_other", "ssis:dataflow.destination_other"),
    "microsoft.recordsetdestination": ("dest_other", "ssis:dataflow.destination_other"),
    "microsoft.lookup": ("lookup", "ssis:dataflow.lookup"),
    "microsoft.mergejoin": ("merge_join", "ssis:dataflow.merge_join"),
    "microsoft.aggregate": ("aggregate", "ssis:dataflow.aggregate"),
    "microsoft.conditionalsplit": ("split", "ssis:dataflow.conditional_split"),
    "microsoft.derivedcolumn": ("derived", "ssis:dataflow.derived_column"),
    "microsoft.dataconvert": ("convert", "ssis:dataflow.data_conversion"),
    "microsoft.copymap": ("copy", "ssis:dataflow.copy_column"),
    "microsoft.charactermap": ("charmap", "ssis:dataflow.character_map"),
    "microsoft.sort": ("sort", "ssis:dataflow.sort"),
    "microsoft.unionall": ("union", "ssis:dataflow.union_all"),
    "microsoft.merge": ("union", "ssis:dataflow.merge"),
    "microsoft.multicast": ("pass", "ssis:dataflow.multicast"),
    "microsoft.rowcount": ("pass", "ssis:dataflow.row_count"),
    "microsoft.oledbcommand": ("command", "ssis:dataflow.oledb_command"),
    "microsoft.scd": ("scd", "ssis:dataflow.scd"),
    "microsoft.pivot": ("pivot", "ssis:dataflow.pivot"),
    "microsoft.unpivot": ("unpivot", "ssis:dataflow.unpivot"),
    "microsoft.scriptcomponent": ("script", "ssis:dataflow.script_component"),
    "microsoft.fuzzylookup": ("other", "ssis:dataflow.fuzzy"),
    "microsoft.fuzzygrouping": ("other", "ssis:dataflow.fuzzy"),
    "microsoft.cdcsplitter": ("other", "ssis:dataflow.cdc_splitter"),
    "microsoft.percentagesampling": ("other", "ssis:dataflow.sampling"),
    "microsoft.rowsampling": ("other", "ssis:dataflow.sampling"),
    "microsoft.lineage": ("other", "ssis:dataflow.audit"),
    "microsoft.cachetransform": ("other", "ssis:dataflow.cache_transform"),
}

_CONSTRUCTS = (
    # Packages and projects
    ("package", "entity:Job", "an SSIS package"),
    ("project_manifest", "attribute:project", "the @Project.manifest of a project deployment file (.ispac)"),
    ("connection_manager", "entity:ConfigKey", "a connection manager in a package (OLE DB, ADO.NET, flat file, ...)"),
    ("project_connection_manager", "entity:ConfigKey", "a project connection manager (.conmgr)"),
    ("connection_expression", "unmapped", "a property expression that sets a connection string at run time"),
    ("flat_file_layout", "entity:DataSchema", "the column list of a flat file connection manager"),
    ("package_parameter", "entity:ConfigKey", "a package parameter"),
    ("project_parameter", "entity:ConfigKey", "a project parameter (Project.params)"),
    ("parameter_reference", "relation:REFERENCES_CONFIG", "a reference to a parameter (@[$Project::...], @[$Package::...]) in an expression or binding"),
    ("variable", "attribute:variables", "a user variable of a package or container (its value is never read)"),
    ("property_expression", "unmapped", "a property expression on a task or container (a value set at run time)"),
    ("configuration", "unmapped", "a package configuration (XML file, environment variable, SQL Server table) of the 2005 and 2008 deployment model"),
    ("log_provider", "unmapped", "a log provider"),
    ("event_handler", "unmapped", "an event handler (OnError, OnPostExecute, ...) and the tasks it holds"),
    ("precedence_constraint", "relation:DEPENDS_ON", "a precedence constraint between two executables (success, failure, completion, expression)"),
    # Control flow
    ("task.execute_sql", "behavior:query", "an Execute SQL task with its SQL typed in"),
    ("task.execute_sql_dynamic", "unmapped", "an Execute SQL task whose SQL comes from a variable, a file connection or an expression"),
    ("task.data_flow", "entity:Program", "a Data Flow task"),
    ("task.execute_package", "relation:RUNS", "an Execute Package task naming a package of the same project"),
    ("task.execute_package_external", "unmapped", "an Execute Package task that runs a package named by a file or SQL Server connection"),
    ("task.script", "unmapped", "a Script task (C# or VB code; counted, not read)"),
    ("task.file_system", "unmapped", "a File System task"),
    ("task.ftp", "unmapped", "an FTP task (the host is never read)"),
    ("task.send_mail", "unmapped", "a Send Mail task"),
    ("task.execute_process", "unmapped", "an Execute Process task (the program it starts is not read)"),
    ("task.expression", "unmapped", "an Expression task"),
    ("task.bulk_insert", "unmapped", "a Bulk Insert task"),
    ("task.web_service", "unmapped", "a Web Service task"),
    ("task.xml", "unmapped", "an XML task"),
    ("task.data_profiling", "unmapped", "a Data Profiling task"),
    ("task.cdc_control", "unmapped", "a CDC Control task"),
    ("task.other", "entity:Task", "another task (maintenance plan, Analysis Services, transfer, WMI, third-party)"),
    ("container.sequence", "entity:Task", "a Sequence container"),
    ("container.for_loop", "entity:Task", "a For Loop container"),
    ("container.foreach_loop", "entity:Task", "a Foreach Loop container (its enumerator is an attribute)"),
    # Data flow
    ("dataflow.source", "attribute:transforms", "an OLE DB, ADO.NET or ODBC source reading a table or a SQL command"),
    ("dataflow.source_dynamic", "unmapped", "a source whose table or SQL command is held in a variable"),
    ("dataflow.source_flat_file", "attribute:transforms", "a flat file source"),
    ("dataflow.source_other", "unmapped", "an Excel, raw file, XML or CDC source"),
    ("dataflow.destination", "attribute:transforms", "an OLE DB, ADO.NET, ODBC or SQL Server destination"),
    ("dataflow.destination_dynamic", "unmapped", "a destination whose table is held in a variable"),
    ("dataflow.destination_flat_file", "attribute:transforms", "a flat file destination"),
    ("dataflow.destination_other", "unmapped", "an Excel, raw file or recordset destination"),
    ("dataflow.lookup", "attribute:transforms", "a Lookup: a join to a reference table or query"),
    ("dataflow.merge_join", "attribute:transforms", "a Merge Join"),
    ("dataflow.aggregate", "attribute:transforms", "an Aggregate"),
    ("dataflow.conditional_split", "attribute:transforms", "a Conditional Split: one filter per output"),
    ("dataflow.derived_column", "attribute:transforms", "a Derived Column"),
    ("dataflow.data_conversion", "attribute:transforms", "a Data Conversion"),
    ("dataflow.copy_column", "attribute:transforms", "a Copy Column"),
    ("dataflow.character_map", "attribute:transforms", "a Character Map"),
    ("dataflow.sort", "attribute:transforms", "a Sort, which may remove duplicate sort keys"),
    ("dataflow.union_all", "attribute:transforms", "a Union All"),
    ("dataflow.merge", "attribute:transforms", "a Merge of two sorted inputs"),
    ("dataflow.multicast", "attribute:transforms", "a Multicast: every output carries its input"),
    ("dataflow.row_count", "unmapped", "a Row Count into a variable; rows pass through"),
    ("dataflow.oledb_command", "attribute:transforms", "an OLE DB Command: SQL run for every row"),
    ("dataflow.scd", "unmapped", "a Slowly Changing Dimension component; a candidate for SCD type 2 when it has historical columns"),
    ("dataflow.pivot", "attribute:transforms", "a Pivot"),
    ("dataflow.unpivot", "attribute:transforms", "an Unpivot"),
    ("dataflow.script_component", "unmapped", "a Script Component (C# or VB code; counted, not read)"),
    ("dataflow.fuzzy", "unmapped", "a Fuzzy Lookup or Fuzzy Grouping"),
    ("dataflow.cdc_splitter", "unmapped", "a CDC Splitter"),
    ("dataflow.sampling", "unmapped", "a Percentage or Row Sampling"),
    ("dataflow.audit", "unmapped", "an Audit component"),
    ("dataflow.cache_transform", "unmapped", "a Cache Transform"),
    ("dataflow.other", "unmapped", "another or third-party data flow component (a class id this reader does not know)"),
    ("dataflow.path", "attribute:transforms", "a path from one component's output to another's input"),
    ("dataflow.error_output", "unmapped", "a path from an error output: rows redirected on failure are not followed"),
    ("expression_unread", "unmapped", "an expression this reader does not parse, kept as text in a platform node"),
)
CONSTRUCTS = tuple(Construct(f"ssis:{name}", text, modelled) for name, modelled, text in _CONSTRUCTS)


def task_type(executable_type: str) -> tuple[str, str]:
    """(task type, construct) of an executable by its type."""
    low = (executable_type or "").lower()
    for marker, kind, construct in TASK_TYPES:
        if marker in low:
            return kind, construct
    return "other", "ssis:task.other"
