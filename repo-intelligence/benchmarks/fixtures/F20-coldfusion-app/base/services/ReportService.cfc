component displayname="ReportService" output=false {

    property name="currency" type="string" default="EUR";

    remote array function salesByRegion(required string region) roles="manager" returnformat="json" {
        var rows = [];
        var q = queryExecute(
            "SELECT region, SUM(total) AS total
               FROM sales_daily
              WHERE region = :region
              GROUP BY region",
            {region: {value: arguments.region, cfsqltype: "cf_sql_varchar"}},
            {datasource: "reportdb"}
        );
        for (var row in q) {
            arrayAppend(rows, {region: row.region, total: fmt(row.total)});
        }
        return rows;
    }

    public numeric function weeklyTotal() {
        var q = queryExecute("SELECT SUM(total) AS total FROM sales_weekly", {}, {datasource: "reportdb"});
        return q.total ?: 0;
    }

    private string function fmt(numeric n) {
        return numberFormat(arguments.n, "9,999.99") & " " & variables.currency;
    }
}
