param location string = resourceGroup().location

// The message bus the worker reads billing requests from.
resource bus 'Microsoft.ServiceBus/namespaces@2022-10-01-preview' = {
  name: 'shop-bus'
  location: location
  sku: { name: 'Standard' }
}

resource billing 'Microsoft.ServiceBus/namespaces/queues@2022-10-01-preview' = {
  parent: bus
  name: 'billing'
}

module web './web.bicep' = {
  name: 'web'
}
