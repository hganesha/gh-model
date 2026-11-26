<%@ Page Title="Order history" Language="C#" MasterPageFile="~/Site.Master" AutoEventWireup="true" CodeBehind="Audit.aspx.cs" Inherits="Desk.Audit" %>
<asp:Content ID="Main" ContentPlaceHolderID="MainContent" runat="server">
  <form id="auditForm" runat="server">
    <asp:TextBox ID="OrderNumber" runat="server" />
    <asp:Button ID="Show" runat="server" Text="Show history" OnClick="Show_Click" />
    <asp:SqlDataSource ID="History" runat="server" ConnectionString="<%$ ConnectionStrings:DeskDb %>"
        SelectCommand="SELECT changed_at, changed_by, field, old_value, new_value FROM dbo.OrderHistory WHERE order_number = @number" />
    <asp:GridView ID="HistoryGrid" runat="server" DataSourceID="History" />
  </form>
</asp:Content>
