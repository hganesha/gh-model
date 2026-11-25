"""Database catalogue dumps: the `catalog` platform pack (docs/solution-ir-contract.md, W5).

A catalogue dump is an export of a database's own description of itself, the
`information_schema` views or their equivalent: tables, columns, views, routines,
constraints and privileges. The owner exports it with the database's own tools and
commits it under `.rkp/inputs/catalog/` (a JSON document with one array per view, or
one CSV file per view); the engine never connects to a database.

What the pack produces, all at E1 (a declaration by the database itself):

    Table      {datastore, name}  as `data.sql_ddl` declares it: `name` is `schema.table`,
                                  or `table` alone in the default `public` schema
    DataSchema {kind: "view", name}  a view, as `data.sql_ddl` declares one
    Field      {table, name}      a column of a table or view
    Program    {platform, name}   a routine, named as `data.sql_lineage` names a procedure
    Role       {realm, name}      a grantee; the realm is the database name
    Permission {realm, name}      one privilege on one object, `SELECT on sales.orders`
    Role GRANTS Permission, and Role GRANTS Role for role membership

Definitions (view and routine bodies) are read for the names of the tables they use and
never copied. Column defaults, comments and every field the pack does not know are
ignored, because they can hold values.
"""

from __future__ import annotations

from repo_intel.compile.ontology import EntityKind
from repo_intel.platforms import Construct, PlatformPack, Term
from repo_intel.platforms.catalog.dump import CatalogDump

K = EntityKind

PACK = PlatformPack(
    id="catalog",
    name="Database catalogue dumps",
    prefix="db",
    version="1.1.0",  # 1.1.0: construct occurrences are E1, as the rest of a dump's output
    terms=(
        Term("db:Table", K.TABLE, "lossy", "a base or foreign table; database, schema and table type are attributes"),
        Term("db:View", K.DATA_SCHEMA, "lossy", "a view or materialised view; the tables it reads are an attribute"),
        Term("db:Column", K.FIELD, "lossy", "a column; type, nullability, position and keys are attributes"),
        Term("db:Routine", K.PROGRAM, "lossy", "a stored procedure or function; parameters are an attribute"),
        Term("db:Role", K.ROLE, "exact", "a grantee: a role, a user, or PUBLIC"),
        Term("db:Grant", K.PERMISSION, "lossy", "one privilege on one table, column or routine; grantability is an attribute"),
    ),
    constructs=(
        Construct("db:table", "a row of information_schema.tables that is a table", "entity:Table"),
        Construct("db:view", "a view, from information_schema.tables or .views", "entity:DataSchema"),
        Construct("db:column", "a row of information_schema.columns", "entity:Field"),
        Construct("db:primary_key", "a primary key constraint", "attribute:primary_key"),
        Construct("db:unique_constraint", "a unique constraint", "attribute:unique"),
        Construct("db:foreign_key", "a foreign key constraint and the column it references", "attribute:references"),
        Construct("db:check_constraint", "a check constraint; its condition is not read", "unmapped"),
        Construct("db:view_dependency", "a table a view reads, from its definition or view_table_usage", "attribute:reads"),
        Construct("db:routine", "a stored procedure or function", "entity:Program"),
        Construct("db:routine_parameter", "a routine parameter, by name", "attribute:parameters"),
        Construct("db:routine_lineage", "the tables a routine's definition reads and writes", "relation:READS_FROM"),
        Construct("db:table_grant", "a privilege on a table or view", "relation:GRANTS"),
        Construct("db:column_grant", "a privilege on one column", "relation:GRANTS"),
        Construct("db:routine_grant", "a privilege on a routine", "relation:GRANTS"),
        Construct("db:role_membership", "a role granted to a role or user", "relation:GRANTS"),
        Construct("db:schema", "a schema (namespace)", "unmapped"),
        Construct("db:sequence", "a sequence", "unmapped"),
        Construct("db:trigger", "a trigger; its body is not read", "unmapped"),
        Construct("db:index", "an index", "unmapped"),
        Construct("db:synonym", "a synonym or alias for another object", "unmapped"),
        Construct("db:user_defined_type", "a domain or user-defined type", "unmapped"),
    ),
    extractors=(CatalogDump(),),
    inputs=(".rkp/inputs/catalog/*.json", ".rkp/inputs/catalog/*.csv"),
)
