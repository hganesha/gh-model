<cflogin>
  <cfif structKeyExists(form, "username")>
    <cfquery name="account" datasource="orderdb">
      SELECT u.user_id, u.role_name
      FROM app_users u
      WHERE u.login = <cfqueryparam value="#form.username#" cfsqltype="cf_sql_varchar">
        AND u.active = 1
    </cfquery>
    <cfif account.recordCount EQ 1 AND account.role_name EQ "manager">
      <cfloginuser name="#form.username#" password="#form.password#" roles="clerk,manager">
      <cflocation url="index.cfm" addtoken="no">
    <cfelseif account.recordCount EQ 1>
      <cfloginuser name="#form.username#" password="#form.password#" roles="clerk">
      <cflocation url="index.cfm" addtoken="no">
    </cfif>
  </cfif>
</cflogin>
<html>
<head><title>Sign in</title></head>
<body>
<form name="signin" action="login.cfm" method="post">
  <label>User <input type="text" name="username" required maxlength="40"></label>
  <label>Password <input type="password" name="password" required></label>
  <button type="submit" name="go">Sign in</button>
</form>
</body>
</html>
