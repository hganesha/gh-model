<%@ page contentType="text/html; charset=UTF-8" %>
<%@ taglib prefix="c" uri="http://java.sun.com/jsp/jstl/core" %>
<%@ taglib prefix="form" uri="http://www.springframework.org/tags/form" %>
<%@ taglib prefix="spring" uri="http://www.springframework.org/tags" %>
<%@ taglib prefix="sec" uri="http://www.springframework.org/security/tags" %>
<html>
<head><title>Edit order</title></head>
<body>
<h1><spring:message code="order.edit.title"/></h1>
<form:form modelAttribute="order" action="save" method="post" id="orderForm">
  <form:hidden path="id"/>
  <form:input path="customer" maxlength="60"/>
  <form:input path="quantity"/>
  <form:select path="status" items="${statuses}"/>
  <form:textarea path="notes"/>
  <button type="submit">Save</button>
</form:form>
<sec:authorize access="hasRole('admin')">
  <a href="../admin/purge.jsp?order=${order.id}">Purge</a>
</sec:authorize>
<a href="list.jsp">Back to orders</a>
</body>
</html>
