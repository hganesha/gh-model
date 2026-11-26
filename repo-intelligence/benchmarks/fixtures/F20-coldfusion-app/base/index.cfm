<!--- The order desk's start page --->
<cfinclude template="header.cfm">
<cfoutput>
<h1>Welcome, #encodeForHTML(getAuthUser())#</h1>
<ul>
  <li><a href="orders/list.cfm">Open orders</a></li>
  <li><a href="orders/list.cfm?status=shipped&key=f20-planted-link-key">Shipped orders</a></li>
  <li><a href="orders/edit.cfm?id=0&ref=f20-planted-ref-3c9##top">New order</a></li>
  <cfif IsUserInRole("manager")>
  <li><a href="reports/sales.cfm">Sales by region</a></li>
  </cfif>
  <li><a href="https://f20-planted-user@help.example.com/orderdesk?ticket=f20-planted-ticket-41">Help</a></li>
</ul>
</cfoutput>
