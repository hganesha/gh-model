<%@ page contentType="text/html; charset=UTF-8" %>
<%@ taglib prefix="c" uri="http://java.sun.com/jsp/jstl/core" %>
<html>
<head><title>Edit customer</title></head>
<body>
<form id="customerForm" action="<c:url value='/customers/save'/>" method="post">
  <input type="hidden" name="customerId" value="${customer.id}" />
  <input type="email" name="email" value="${customer.email}" required />
  <input type="text" name="phone" pattern="[0-9 +]+" />
  <button type="submit">Save customer</button>
</form>
<a href="../index.jsp">Back</a>
</body>
</html>
