<cfinclude template="header.cfm">
<cfset svc = createObject("component", "services.OrderService")>
<cfquery name="recent" datasource="shopdb">
  SELECT o.id, c.name, o.total
  FROM orders o JOIN customers c ON c.id = o.customer_id
  ORDER BY o.id DESC
</cfquery>
<cfoutput query="recent"><p>#name#: #total#</p></cfoutput>
