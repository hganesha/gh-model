component output="false" {
    public void function record(required string table) {
        queryExecute(
            "INSERT INTO audit_log (table_name, changed_at) VALUES (:t, CURRENT_TIMESTAMP)",
            {t = arguments.table},
            {datasource = "auditdb"}
        );
    }
}
