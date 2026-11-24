<cfcomponent displayname="OrderService" output="false">
  <!--- Orders for the storefront and the call centre --->
  <cffunction name="getOrder" access="remote" returntype="struct">
    <cfargument name="id" type="numeric" required="true">
    <cfquery name="q" datasource="shopdb">
      SELECT id, customer_id, total FROM orders WHERE id = <cfqueryparam value="#arguments.id#" cfsqltype="cf_sql_integer">
    </cfquery>
    <cfreturn q>
  </cffunction>

  <cffunction name="placeOrder" access="public" returntype="numeric">
    <cfargument name="customer" type="numeric" required="true">
    <cfquery name="ins" datasource="shopdb" result="r">
      INSERT INTO orders (customer_id, total) VALUES (<cfqueryparam value="#arguments.customer#">, 0)
    </cfquery>
    <cfinvoke component="services.AuditService" method="record" table="orders">
    <cfreturn r.generatedKey>
  </cffunction>
</cfcomponent>
