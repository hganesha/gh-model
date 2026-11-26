<cfif NOT structKeyExists(session, "lastSaved") AND NOT isUserLoggedIn()>
  <cflocation url="../login.cfm?token=f20-planted-token-5d1e&sid=f20-planted-sid-77##relogin" addtoken="no">
</cfif>
<cfset svc = new services.OrderService()>
<cftry>
  <cfset svc.saveOrder(form.orderId, form.qty, form.status, form.notes)>
  <cfcatch type="database">
    <cflog file="orderdesk" text="save failed for order #form.orderId#">
    <cflocation url="edit.cfm?id=#form.orderId#&error=1" addtoken="no">
  </cfcatch>
</cftry>
<cfif structKeyExists(session, "returnTo")>
  <cflocation url="#session.returnTo#" addtoken="no">
</cfif>
<cflocation url="list.cfm?saved=1" addtoken="no">
