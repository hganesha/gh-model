using Microsoft.EntityFrameworkCore;

public class BillingDb : DbContext
{
    public DbSet<Invoice> Invoices { get; set; }
}

public class Invoice
{
    public int Id { get; set; }
}
