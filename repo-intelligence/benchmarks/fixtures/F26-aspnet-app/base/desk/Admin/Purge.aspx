<%@ Page Title="Purge orders" Language="C#" MasterPageFile="~/Site.Master" AutoEventWireup="true" CodeBehind="Purge.aspx.cs" Inherits="Desk.Admin.Purge" %>
<asp:Content ID="Main" ContentPlaceHolderID="MainContent" runat="server">
  <form id="purgeForm" runat="server">
    <asp:TextBox ID="OlderThanDays" runat="server" />
    <asp:CheckBox ID="DryRun" runat="server" Checked="true" />
    <asp:Button ID="Purge" runat="server" Text="Purge" OnClick="Purge_Click" />
  </form>
</asp:Content>
