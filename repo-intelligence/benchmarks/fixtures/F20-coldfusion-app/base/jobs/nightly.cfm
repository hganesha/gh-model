<!--- Nightly clean-up, scheduled by admin/schedule.cfm --->
<cfsetting requesttimeout="600">
<cfquery datasource="orderdb">
  DELETE FROM order_drafts
  WHERE created_on < <cfqueryparam value="#dateAdd('d', -7, now())#" cfsqltype="cf_sql_timestamp">
</cfquery>
<cflog file="orderdesk" text="nightly clean-up done">
