<%@ Page Title="Edit order" Language="C#" MasterPageFile="~/Site.Master" AutoEventWireup="true" CodeBehind="Edit.aspx.cs" Inherits="Desk.Orders.Edit" %>
<%@ Register TagPrefix="ajax" Namespace="AjaxControlToolkit" Assembly="AjaxControlToolkit" %>
<asp:Content ID="Main" ContentPlaceHolderID="MainContent" runat="server">
  <form id="orderForm" runat="server">
    <asp:HiddenField ID="OrderId" runat="server" />
    <asp:TextBox ID="Quantity" runat="server" MaxLength="4" />
    <asp:RequiredFieldValidator ID="QuantityRequired" runat="server" ControlToValidate="Quantity" ErrorMessage="Enter a quantity" />
    <asp:RangeValidator ID="QuantityRange" runat="server" ControlToValidate="Quantity" MinimumValue="1" MaximumValue="999" Type="Integer" />
    <asp:DropDownList ID="Product" runat="server" DataSourceID="Products" DataTextField="Name" DataValueField="ProductId" />
    <asp:TextBox ID="Notes" runat="server" TextMode="MultiLine" />
    <ajax:CalendarExtender ID="DueCalendar" runat="server" TargetControlID="Notes" />
    <asp:Button ID="Save" runat="server" Text="Save" OnClick="Save_Click" />
    <asp:Button ID="Review" runat="server" Text="Review" PostBackUrl="~/Orders/Review.aspx" />
    <asp:SqlDataSource ID="Products" runat="server" ConnectionString="<%$ ConnectionStrings:DeskDb %>"
        SelectCommand="SELECT ProductId, Name FROM dbo.Products WHERE Active = @active" />
  </form>
  <% if (!User.IsInRole("Clerk") && !User.IsInRole("Admin")) { Response.Redirect("~/Login.aspx?ReturnUrl=orders"); } %>
  <% Server.Transfer(Request.QueryString["next"]); %>
</asp:Content>
