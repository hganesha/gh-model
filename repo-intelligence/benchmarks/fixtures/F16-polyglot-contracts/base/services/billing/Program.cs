using Azure.Messaging.ServiceBus;

var builder = WebApplication.CreateBuilder(args);
var app = builder.Build();

var api = app.MapGroup("/api");
api.MapPost("/invoices", async (Invoice invoice, BillingDb db, ServiceBusClient bus) =>
{
    db.Invoices.Add(invoice);
    await db.SaveChangesAsync();
    await bus.CreateSender("invoices").SendMessageAsync(new ServiceBusMessage(invoice.Id.ToString()));
    return Results.Created($"/api/invoices/{invoice.Id}", invoice);
});
api.MapGet("/invoices/{id}", (int id, BillingDb db) => db.Invoices.Find(id));

app.Run();
