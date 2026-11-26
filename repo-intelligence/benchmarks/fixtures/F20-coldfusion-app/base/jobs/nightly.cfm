<!--- Nightly clean-up, scheduled by admin/schedule.cfm --->
<cfsetting requesttimeout="600">
<cfquery datasource="orderdb">
  DELETE FROM order_drafts
  WHERE created_on < <cfqueryparam value="#dateAdd('d', -7, now())#" cfsqltype="cf_sql_timestamp">
</cfquery>
<cfhttp url="https://hooks.example.com/notify/orders?api_key=f20-planted-api-key##done" method="post" result="hook">
  <cfhttpparam type="header" name="Authorization" value="Bearer f20-planted-bearer">
  <cfhttpparam type="url" name="client_secret" value="f20-planted-param-secret">
</cfhttp>
<cflog file="orderdesk" text="nightly clean-up done">
