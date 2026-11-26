<%@ Page Title="Order kiosk" Language="C#" AutoEventWireup="true" CodeBehind="Default.aspx.cs" Inherits="Kiosk._Default" %>
<html>
<body>
  <form id="lookupForm" runat="server">
    <asp:TextBox ID="OrderNumber" runat="server" />
    <asp:Button ID="Lookup" runat="server" Text="Find my order" OnClick="Lookup_Click" />
  </form>
</body>
</html>
