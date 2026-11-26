<%@ page contentType="text/html; charset=UTF-8" import="java.util.Date" %>
<%@ taglib prefix="c" uri="http://java.sun.com/jsp/jstl/core" %>
<%@ taglib prefix="fmt" uri="http://java.sun.com/jsp/jstl/fmt" %>
<%@ taglib prefix="sql" uri="http://java.sun.com/jsp/jstl/sql" %>
<%
  if (!request.isUserInRole("manager")) {
    response.sendRedirect(request.getContextPath() + "/login.jsp");
    return;
  }
%>
<sql:query var="sales" dataSource="jdbc/OrderDesk">
  SELECT region, SUM(total) AS total FROM sales_daily GROUP BY region
</sql:query>
<html>
<head><title>Sales by region</title></head>
<body>
<c:forEach var="r" items="${sales.rows}">
  <p><c:out value="${r.region}"/>: <fmt:formatNumber value="${r.total}" type="currency"/></p>
</c:forEach>
<jsp:include page="${param.extra}" />
</body>
</html>
