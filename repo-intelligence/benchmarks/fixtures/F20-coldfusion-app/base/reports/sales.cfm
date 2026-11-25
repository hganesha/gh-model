<cfif NOT IsUserInRole("manager")>
  <cflocation url="../index.cfm" addtoken="no">
  <cfabort>
</cfif>
<cfinclude template="../header.cfm">
<cfparam name="form.region" default="north">
<cfset reports = createObject("component", "services.ReportService")>
<cfset rows = reports.salesByRegion(form.region)>
<form name="regionPicker" method="post">
  <select name="region">
    <option>north</option>
    <option>south</option>
  </select>
  <button type="submit">Show</button>
</form>
<cfoutput>
<cfloop array="#rows#" index="row">
  <p>#row.region#: #row.total#</p>
</cfloop>
</cfoutput>
<cfif structKeyExists(url, "extra")>
  <cfinclude template="#url.extra#.cfm">
</cfif>
