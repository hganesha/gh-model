<%@ Page Title="Review order" Language="C#" MasterPageFile="~/Site.Master" AutoEventWireup="true" CodeBehind="Review.aspx.cs" Inherits="Desk.Orders.Review" %>
<%@ PreviousPageType VirtualPath="~/Orders/Edit.aspx" %>
<asp:Content ID="Main" ContentPlaceHolderID="MainContent" runat="server">
  <form id="reviewForm" runat="server">
    <asp:Label ID="Summary" runat="server" />
    <asp:Button ID="Confirm" runat="server" Text="Confirm" OnClick="Confirm_Click" />
    <asp:LinkButton ID="Back" runat="server" Text="Back" PostBackUrl="~/Orders/Edit.aspx" />
  </form>
  <script runat="server">
    protected void Confirm_Click(object sender, EventArgs e)
    {
        Response.Redirect("~/Default.aspx");
    }
  </script>
</asp:Content>
