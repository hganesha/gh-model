<cfcomponent displayname="OrderService" output="false">

  <cffunction name="getOrder" access="remote" returntype="struct" roles="clerk,manager" output="false">
    <cfargument name="orderId" type="numeric" required="true">
    <cfset var result = {}>
    <cfquery name="q" datasource="orderdb">
      SELECT order_id, qty, status, notes
      FROM orders
      WHERE order_id = <cfqueryparam value="#arguments.orderId#" cfsqltype="cf_sql_integer">
    </cfquery>
    <cfif q.recordCount>
      <cfset result = {id = q.order_id, qty = q.qty, status = q.status, notes = q.notes}>
    </cfif>
    <cfreturn result>
  </cffunction>

  <cffunction name="saveOrder" access="public" returntype="void" output="false">
    <cfargument name="orderId" type="numeric" required="true">
    <cfargument name="qty" type="numeric" required="true">
    <cfargument name="status" type="string" default="open">
    <cfargument name="notes" type="string" default="">
    <cfif arguments.qty LT 1>
      <cfthrow type="Validation" message="Quantity must be positive">
    </cfif>
    <cflock scope="session" type="exclusive" timeout="5">
      <cfset session.lastSaved = arguments.orderId>
    </cflock>
    <cftransaction>
      <cfquery datasource="orderdb">
        UPDATE orders
        SET qty = <cfqueryparam value="#arguments.qty#" cfsqltype="cf_sql_integer">,
            status = <cfqueryparam value="#arguments.status#" cfsqltype="cf_sql_varchar">
        WHERE order_id = <cfqueryparam value="#arguments.orderId#" cfsqltype="cf_sql_integer">
      </cfquery>
      <cfquery datasource="orderdb">
        INSERT INTO order_history (order_id, changed_by, notes)
        VALUES (
          <cfqueryparam value="#arguments.orderId#" cfsqltype="cf_sql_integer">,
          <cfqueryparam value="#getAuthUser()#" cfsqltype="cf_sql_varchar">,
          <cfqueryparam value="#arguments.notes#" cfsqltype="cf_sql_longvarchar">
        )
      </cfquery>
    </cftransaction>
    <cfset notify(arguments.orderId)>
  </cffunction>

  <cffunction name="notify" access="private" returntype="void" output="false">
    <cfargument name="orderId" type="numeric" required="true">
    <cfmail to="orders@example.com" from="desk@example.com" subject="Order #arguments.orderId# changed" type="text">
Order #arguments.orderId# was changed by #getAuthUser()#.
    </cfmail>
  </cffunction>

  <cffunction name="auditTrail" access="remote" returntype="query" roles="auditor" output="false">
    <cfargument name="orderId" type="numeric" required="true">
    <cfquery name="local.history" datasource="orderdb">
      SELECT changed_on, changed_by, notes
      FROM order_history
      WHERE order_id = <cfqueryparam value="#arguments.orderId#" cfsqltype="cf_sql_integer">
      ORDER BY changed_on
    </cfquery>
    <cfreturn local.history>
  </cffunction>

</cfcomponent>
