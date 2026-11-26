<%@ Control Language="C#" AutoEventWireup="true" CodeBehind="Header.ascx.cs" Inherits="Desk.Controls.Header" %>
<div class="header">
  <asp:HyperLink ID="HomeLink" runat="server" NavigateUrl="~/Default.aspx" Text="Order desk" />
  <asp:LoginView ID="AdminMenu" runat="server">
    <RoleGroups>
      <asp:RoleGroup Roles="Admin">
        <ContentTemplate><a href="../Admin/Purge.aspx">Administration</a></ContentTemplate>
      </asp:RoleGroup>
    </RoleGroups>
  </asp:LoginView>
  <asp:LoginStatus ID="Status" runat="server" />
</div>
