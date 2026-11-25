<cfif NOT IsUserInRole("manager")>
  <cfabort>
</cfif>
<cfschedule action="update"
            task="nightly-cleanup"
            operation="HTTPRequest"
            url="http://localhost:8500/jobs/nightly.cfm"
            startdate="01/01/2026"
            starttime="02:00"
            interval="daily">
<cfoutput><p>Scheduled.</p></cfoutput>
