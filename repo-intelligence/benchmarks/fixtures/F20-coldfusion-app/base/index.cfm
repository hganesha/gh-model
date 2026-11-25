<!--- The order desk's start page --->
<cfinclude template="header.cfm">
<cfoutput>
<h1>Welcome, #encodeForHTML(getAuthUser())#</h1>
<ul>
  <li><a href="orders/list.cfm">Open orders</a></li>
  <li><a href="orders/edit.cfm?id=0">New order</a></li>
  <cfif IsUserInRole("manager")>
  <li><a href="reports/sales.cfm">Sales by region</a></li>
  </cfif>
  <li><a href="https://help.example.com/orderdesk">Help</a></li>
</ul>
</cfoutput>
