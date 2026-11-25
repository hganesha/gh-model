<cfinclude template="../header.cfm">
<cfparam name="url.id" default="0" type="integer">
<cfinvoke component="services.OrderService" method="getOrder" returnvariable="order">
  <cfinvokeargument name="orderId" value="#url.id#">
</cfinvoke>
<cfform name="orderForm" action="save.cfm" method="post">
  <cfinput type="hidden" name="orderId" value="#order.id#">
  <label>Quantity <cfinput type="text" name="qty" value="#order.qty#" required="yes" validate="integer" message="Enter a whole number"></label>
  <label>Status
    <cfselect name="status" required="yes">
      <option value="open">Open</option>
      <option value="shipped">Shipped</option>
    </cfselect>
  </label>
  <label>Notes <cftextarea name="notes" maxlength="500"></cftextarea></label>
  <cfinput type="submit" name="save" value="Save">
</cfform>
<a href="list.cfm">Back to the list</a>
