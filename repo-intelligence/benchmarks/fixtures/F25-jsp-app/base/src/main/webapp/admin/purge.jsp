<%@ page contentType="text/html; charset=UTF-8" %>
<%@ taglib prefix="html" uri="http://struts.apache.org/tags-html" %>
<%@ taglib prefix="bean" uri="http://struts.apache.org/tags-bean" %>
<%@ taglib prefix="logic" uri="http://struts.apache.org/tags-logic" %>
<html>
<head><title>Purge orders</title></head>
<body>
<logic:present role="admin">
<html:form action="/admin/purge" method="post">
  <html:text property="olderThanDays"/>
  <html:checkbox property="dryRun"/>
  <html:submit value="Purge"/>
</html:form>
</logic:present>
<p><bean:message key="purge.warning"/></p>
</body>
</html>
