<!--- Custom tag: <cf_pager total="n" page="p"> --->
<cfparam name="attributes.total" type="numeric">
<cfparam name="attributes.page" default="1" type="numeric">
<cfif thisTag.executionMode EQ "start">
  <cfset pages = ceiling(attributes.total / 25)>
  <cfoutput>
  <cfloop from="1" to="#pages#" index="p">
    <cfif p EQ attributes.page><strong>#p#</strong><cfelse><a href="?page=#p#">#p#</a></cfif>
  </cfloop>
  </cfoutput>
</cfif>
