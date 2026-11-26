<%@ page contentType="text/html; charset=UTF-8" errorPage="/error.jsp" %>
<%@ taglib prefix="c" uri="http://java.sun.com/jsp/jstl/core" %>
<%@ taglib prefix="fmt" uri="http://java.sun.com/jsp/jstl/fmt" %>
<%@ include file="/common/header.jspf" %>
<html>
<head><title>Order desk</title></head>
<body>
<h1><fmt:message key="desk.welcome"/></h1>
<form action="orders/list.jsp" method="get">
  <input type="text" name="customer" />
  <select name="status">
    <option value="open">Open</option>
    <option value="closed">Closed</option>
  </select>
  <button type="submit">Search</button>
</form>
<a href="orders/edit.jsp">New order</a>
<a href="reports/sales">Sales report</a>
<a href="https://help.example.com/desk?topic=start">Help</a>
</body>
</html>
