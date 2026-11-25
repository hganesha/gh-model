component output=false {

    remote struct function lookup(required numeric id) roles="clerk,manager" returnformat="json" {
        var q = queryExecute(
            "SELECT customer_id, display_name, email, tier FROM customers WHERE customer_id = :id",
            {id: {value: arguments.id, cfsqltype: "cf_sql_integer"}},
            {datasource: "orderdb"}
        );
        if (q.recordCount == 0) {
            throw(type = "NotFound", message = "No customer #arguments.id#");
        }
        return {name: q.display_name, email: q.email, tier: q.tier};
    }

    remote void function save(required numeric id, required string name, string email = "", string notes = "") roles="clerk" {
        transaction {
            queryExecute(
                "UPDATE customers SET display_name = :name, email = :email WHERE customer_id = :id",
                {id: arguments.id, name: arguments.name, email: arguments.email},
                {datasource: "orderdb"}
            );
        }
        var tidy = (s) => trim(s);
        local.cleanNotes = tidy(arguments.notes);
        location(url = "/customers/edit.cfm?id=#arguments.id#", addToken = false);
    }
}
