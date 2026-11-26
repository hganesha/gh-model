<%@ page contentType="text/html; charset=UTF-8" %>
<%@ taglib prefix="c" uri="http://java.sun.com/jsp/jstl/core" %>
<%@ taglib prefix="sql" uri="http://java.sun.com/jsp/jstl/sql" %>
<%@ taglib prefix="desk" tagdir="/WEB-INF/tags" %>
<sql:query var="orders" dataSource="jdbc/OrderDesk">
  SELECT o.order_id, o.customer, o.total FROM orders o
  WHERE o.customer LIKE ? AND o.status = ?
  <sql:param value="${param.customer}%"/>
  <sql:param value="${param.status}"/>
</sql:query>
<html>
<head><title>Orders</title></head>
<body>
<table>
<c:forEach var="row" items="${orders.rows}">
  <tr>
    <td><c:out value="${row.customer}"/></td>
    <td><desk:money amount="${row.total}"/></td>
    <td><a href="edit.jsp?id=${row.order_id}">Edit</a></td>
  </tr>
</c:forEach>
</table>
<c:if test="${empty orders.rows}">
  <p>No orders.</p>
</c:if>
</body>
</html>
