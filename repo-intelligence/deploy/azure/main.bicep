// RKP on Azure, small footprint.
//
//   Explorer (Container App, public, optional Entra/OIDC sign-in and IP allow-list, scales to zero)
//     -> API (Container App, internal only, one replica)
//          -> PostgreSQL Flexible Server (Burstable B1ms)
//          -> Azure Files share mounted at /data (object store, git mirrors, providers.env)
//   One user-assigned identity pulls images from ACR and signs in to Azure model deployments.
//
// Deployed twice by deploy.sh: first with deployApps=false (infrastructure, so images
// can be built into the registry), then with deployApps=true and the image tags.

targetScope = 'resourceGroup'

@description('Short prefix for resource names (lowercase letters and digits).')
@minLength(2)
@maxLength(8)
param prefix string = 'rkp'
param location string = resourceGroup().location

@description('Create the two container apps. False on the first pass, before images exist.')
param deployApps bool = false
param apiImageTag string = ''
param explorerImageTag string = ''

@description('Addresses (CIDR) allowed to open the Explorer. Empty means open to the internet.')
param allowedIpRanges array = []

@secure()
@description('PostgreSQL administrator password. deploy.sh generates a new one on every run.')
param postgresAdminPassword string
@secure()
@description('Password of the restricted application role the API connects as.')
param appDbPassword string
@secure()
@description('Bearer token shared by the Explorer proxy and the API.')
param apiToken string
@secure()
@description('Optional GitHub token, mounted for the API as /secrets/github-token (credential reference managed:github-token).')
param githubToken string = ''

@description('Sign-in for the Explorer: an Entra tenant ID (sets the issuer), or any OIDC issuer URL. Both empty: no sign-in, and the IP allow-list is the only protection.')
param entraTenantId string = ''
param oidcIssuer string = ''
param oidcClientId string = ''
@secure()
param oidcClientSecret string = ''
@secure()
@description('Signs Explorer session cookies. deploy.sh generates a new one on every run, which signs everyone out.')
param sessionSecret string = ''
@description('Comma-separated app roles or group IDs that may change things; everyone else signs in read-only.')
param oidcAdminRoles string = ''

param apiCpu string = '0.5'
param apiMemory string = '1Gi'

var suffix = uniqueString(resourceGroup().id)
var names = {
  identity: '${prefix}-id'
  logs: '${prefix}-logs'
  registry: toLower('${prefix}acr${suffix}')
  storage: toLower(take('${prefix}st${suffix}', 24))
  share: 'rkp-data'
  postgres: '${prefix}-pg-${suffix}'
  environment: '${prefix}-env'
  api: '${prefix}-api'
  explorer: '${prefix}-explorer'
}
var postgresAdmin = 'rkpadmin'
var acrPullRole = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: names.identity
  location: location
}

resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: names.logs
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: names.registry
  location: location
  sku: { name: 'Basic' }
  properties: { adminUserEnabled: false }
}

resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, identity.id, acrPullRole)
  scope: registry
  properties: {
    roleDefinitionId: acrPullRole
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: names.storage
  location: location
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    supportsHttpsTrafficOnly: true
  }
}

resource share 'Microsoft.Storage/storageAccounts/fileServices/shares@2023-05-01' = {
  name: '${storage.name}/default/${names.share}'
  properties: { shareQuota: 100 }
}

resource postgres 'Microsoft.DBforPostgreSQL/flexibleServers@2022-12-01' = {
  name: names.postgres
  location: location
  sku: {
    name: 'Standard_B1ms'
    tier: 'Burstable'
  }
  properties: {
    version: '16'
    administratorLogin: postgresAdmin
    administratorLoginPassword: postgresAdminPassword
    storage: { storageSizeGB: 32 }
    backup: {
      backupRetentionDays: 7
      geoRedundantBackup: 'Disabled'
    }
    highAvailability: { mode: 'Disabled' }
  }
}

// Public endpoint, reachable only from Azure services; TLS is required by default.
resource postgresFromAzure 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2022-12-01' = {
  parent: postgres
  name: 'AllowAzureServices'
  properties: {
    startIpAddress: '0.0.0.0'
    endIpAddress: '0.0.0.0'
  }
}

resource environment 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: names.environment
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logs.properties.customerId
        sharedKey: logs.listKeys().primarySharedKey
      }
    }
  }
}

resource dataStorage 'Microsoft.App/managedEnvironments/storages@2024-03-01' = {
  parent: environment
  name: 'rkp-data'
  properties: {
    azureFile: {
      accountName: storage.name
      accountKey: storage.listKeys().keys[0].value
      shareName: names.share
      accessMode: 'ReadWrite'
    }
  }
  dependsOn: [ share ]
}

var registryLogin = [
  {
    server: registry.properties.loginServer
    identity: identity.id
  }
]
var hasGithubToken = !empty(githubToken)
var signIn = !empty(entraTenantId) || !empty(oidcIssuer)
// Known before the Explorer exists, so the app registration's redirect URIs can be set first.
var explorerUrl = 'https://${names.explorer}.${environment.properties.defaultDomain}'

resource api 'Microsoft.App/containerApps@2024-03-01' = if (deployApps) {
  name: names.api
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identity.id}': {} }
  }
  properties: {
    environmentId: environment.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: false // reachable only from inside the environment (the Explorer)
        targetPort: 8787
        transport: 'http'
        allowInsecure: true
      }
      registries: registryLogin
      secrets: concat([
        {
          name: 'database-url'
          value: 'host=${postgres.properties.fullyQualifiedDomainName} port=5432 dbname=postgres user=${postgresAdmin} password=${postgresAdminPassword} sslmode=require'
        }
        { name: 'app-db-password', value: appDbPassword }
        { name: 'api-token', value: apiToken }
      ], hasGithubToken ? [ { name: 'github-token', value: githubToken } ] : [])
    }
    template: {
      containers: [
        {
          name: 'api'
          image: '${registry.properties.loginServer}/rkp-api:${apiImageTag}'
          resources: {
            cpu: json(apiCpu)
            memory: apiMemory
          }
          env: [
            { name: 'RKP_DATABASE_URL', secretRef: 'database-url' }
            { name: 'RKP_APP_DB_PASSWORD', secretRef: 'app-db-password' }
            { name: 'RKP_DEV_TOKEN', secretRef: 'api-token' }
            { name: 'RKP_DATA_DIR', value: '/data' }
            { name: 'RKP_MANAGED_SECRET_DIR', value: '/secrets' }
            { name: 'AZURE_CLIENT_ID', value: identity.properties.clientId }
          ]
          volumeMounts: concat([ { volumeName: 'data', mountPath: '/data' } ], hasGithubToken ? [ { volumeName: 'secrets', mountPath: '/secrets' } ] : [])
          probes: [
            {
              type: 'Startup'
              tcpSocket: { port: 8787 }
              periodSeconds: 5
              failureThreshold: 60
            }
          ]
        }
      ]
      // Compile jobs and repository polling run in this process: exactly one replica.
      scale: {
        minReplicas: 1
        maxReplicas: 1
      }
      volumes: concat([
        {
          name: 'data'
          storageType: 'AzureFile'
          storageName: dataStorage.name
          mountOptions: 'uid=10001,gid=10001,dir_mode=0700,file_mode=0600'
        }
      ], hasGithubToken ? [
        {
          name: 'secrets'
          storageType: 'Secret'
          secrets: [ { secretRef: 'github-token', path: 'github-token' } ]
        }
      ] : [])
    }
  }
  dependsOn: [ acrPull, postgresFromAzure ]
}

resource explorer 'Microsoft.App/containerApps@2024-03-01' = if (deployApps) {
  name: names.explorer
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identity.id}': {} }
  }
  properties: {
    environmentId: environment.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 3000
        transport: 'auto'
        allowInsecure: false
        ipSecurityRestrictions: [for (range, i) in allowedIpRanges: {
          name: 'allow-${i}'
          ipAddressRange: range
          action: 'Allow'
        }]
      }
      registries: registryLogin
      secrets: concat([ { name: 'api-token', value: apiToken } ], signIn ? [
        { name: 'oidc-client-secret', value: oidcClientSecret }
        { name: 'session-secret', value: sessionSecret }
      ] : [])
    }
    template: {
      containers: [
        {
          name: 'explorer'
          image: '${registry.properties.loginServer}/rkp-explorer:${explorerImageTag}'
          resources: {
            cpu: json('0.25')
            memory: '0.5Gi'
          }
          env: concat([
            { name: 'RKP_API_URL', value: 'http://${names.api}' }
            { name: 'RKP_DEV_TOKEN', secretRef: 'api-token' }
          ], signIn ? [
            { name: empty(entraTenantId) ? 'RKP_OIDC_ISSUER' : 'RKP_ENTRA_TENANT_ID', value: empty(entraTenantId) ? oidcIssuer : entraTenantId }
            { name: 'RKP_OIDC_CLIENT_ID', value: oidcClientId }
            { name: 'RKP_OIDC_CLIENT_SECRET', secretRef: 'oidc-client-secret' }
            { name: 'RKP_SESSION_SECRET', secretRef: 'session-secret' }
            { name: 'RKP_PUBLIC_URL', value: explorerUrl }
            { name: 'RKP_OIDC_ADMIN_ROLES', value: oidcAdminRoles }
          ] : [])
        }
      ]
      scale: {
        minReplicas: 0
        maxReplicas: 1
      }
    }
  }
  dependsOn: [ api, acrPull ]
}

output registryName string = registry.name
output registryServer string = registry.properties.loginServer
output storageAccount string = storage.name
output shareName string = names.share
output identityPrincipalId string = identity.properties.principalId
output postgresServer string = postgres.name
output explorerUrl string = explorerUrl
