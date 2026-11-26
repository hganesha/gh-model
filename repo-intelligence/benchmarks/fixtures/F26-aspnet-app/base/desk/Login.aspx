<%@ Page Title="Sign in" Language="C#" AutoEventWireup="true" CodeBehind="Login.aspx.cs" Inherits="Desk.Login" %>
<!DOCTYPE html>
<html>
<head runat="server"><title>Sign in</title></head>
<body>
  <form id="loginForm" runat="server">
    <asp:Login ID="SignIn" runat="server" DestinationPageUrl="~/Default.aspx" />
  </form>
</body>
</html>
