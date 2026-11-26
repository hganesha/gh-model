<%@ Page Title="Order desk" Language="C#" MasterPageFile="~/Site.Master" AutoEventWireup="true" CodeBehind="Default.aspx.cs" Inherits="Desk._Default" %>
<asp:Content ID="Main" ContentPlaceHolderID="MainContent" runat="server">
  <form id="searchForm" runat="server">
    <asp:TextBox ID="Customer" runat="server" />
    <asp:DropDownList ID="Status" runat="server">
      <asp:ListItem Value="open">Open</asp:ListItem>
      <asp:ListItem Value="closed">Closed</asp:ListItem>
    </asp:DropDownList>
    <asp:Button ID="Search" runat="server" Text="Search" OnClick="Search_Click" />
    <asp:GridView ID="Results" runat="server" AutoGenerateColumns="true" />
  </form>
  <asp:HyperLink ID="NewOrder" runat="server" NavigateUrl="~/Orders/Edit.aspx" Text="New order" />
  <a href="https://help.example.com/desk?topic=start">Help</a>
</asp:Content>
