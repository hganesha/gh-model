<%@ tag body-content="empty" %>
<%@ attribute name="amount" required="true" type="java.math.BigDecimal" %>
<%@ taglib prefix="fmt" uri="http://java.sun.com/jsp/jstl/fmt" %>
<fmt:formatNumber value="${amount}" type="currency" currencyCode="EUR"/>
