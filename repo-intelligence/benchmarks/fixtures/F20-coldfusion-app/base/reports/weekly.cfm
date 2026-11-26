<!--- Requested by the Administrator's scheduler each Monday (neo-cron.xml) --->
<cfsetting enablecfoutputonly="true">
<cfset total = createObject("component", "services.ReportService").weeklyTotal()>
<cfmail to="sales@example.com" from="desk@example.com" subject="Weekly sales">
Weekly sales: #total#
</cfmail>
