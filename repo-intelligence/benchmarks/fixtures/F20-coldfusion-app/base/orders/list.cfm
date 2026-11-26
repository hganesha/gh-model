<cfinclude template="../header.cfm">
<cfparam name="url.status" default="open" type="string">
<cfquery name="orders" datasource="orderdb">
  SELECT o.order_id, o.placed_on, c.display_name, o.total
  FROM orders o
  INNER JOIN customers c ON c.customer_id = o.customer_id
  WHERE o.status = <cfqueryparam value="#url.status#" cfsqltype="cf_sql_varchar">
  ORDER BY o.placed_on DESC
</cfquery>
<form name="filter" action="list.cfm?view=f20-planted-view" method="get">
  <select name="status">
    <option value="open">Open</option>
    <option value="shipped">Shipped</option>
  </select>
  <input type="submit" value="Filter">
</form>
<table>
<cfoutput query="orders">
  <tr>
    <td><a href="edit.cfm?id=#orders.order_id#">#orders.order_id#</a></td>
    <td>#dateFormat(orders.placed_on, "yyyy-mm-dd")#</td>
    <td>#encodeForHTML(orders.display_name)#</td>
    <td>#numberFormat(orders.total, "9,999.99")#</td>
  </tr>
</cfoutput>
</table>
<cf_pager total="#orders.recordCount#" page="1">
