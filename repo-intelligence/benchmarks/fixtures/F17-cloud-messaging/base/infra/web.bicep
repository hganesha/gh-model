param location string = resourceGroup().location

resource plan 'Microsoft.Web/serverfarms@2023-01-01' = {
  name: 'shop-plan'
  location: location
  sku: { name: 'P1v3' }
}
