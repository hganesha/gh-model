<cfinclude template="../header.cfm">
<cfparam name="url.id" default="">
<cfscript>
    customers = new services.CustomerService();
    customer = len(url.id) ? customers.lookup(url.id) : {name: "", email: ""};
    switch (customer.tier ?: "standard") {
        case "gold":
            badge = "Gold customer";
            break;
        default:
            badge = "";
    }
</cfscript>
<cfoutput>
<h1>#encodeForHTML(customer.name)# <small>#badge#</small></h1>
<form name="customerForm" action="../services/CustomerService.cfc?method=save" method="post">
  <input type="hidden" name="id" value="#encodeForHTMLAttribute(url.id)#">
  <label>Name <input type="text" name="name" value="#encodeForHTMLAttribute(customer.name)#" required></label>
  <label>E-mail <input type="email" name="email" value="#encodeForHTMLAttribute(customer.email)#"></label>
  <textarea name="notes" rows="4"></textarea>
  <button type="submit" name="saveCustomer">Save</button>
</form>
</cfoutput>
