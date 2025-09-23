// Resources module for azd-mcp-client deployment
targetScope = 'resourceGroup'

// Parameters
@minLength(1)
@description('Primary location for all resources')
param location string

@description('Resource token for unique naming')
param resourceToken string

@description('Resource prefix for naming')
param resourcePrefix string

@description('Tags for all resources')
param tags object

@description('Name of the OpenAI model to deploy')
param openAIModelName string

@description('API version for OpenAI service')
param openAIAPIVersion string

// Log Analytics Workspace
resource logAnalyticsWorkspace 'Microsoft.OperationalInsights/workspaces@2022-10-01' = {
  name: '${resourcePrefix}-logs-${resourceToken}'
  location: location
  tags: tags
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
  }
}

// Application Insights
resource applicationInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: '${resourcePrefix}-ai-${resourceToken}'
  location: location
  tags: tags
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logAnalyticsWorkspace.id
  }
}

// User-assigned managed identity
resource managedIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${resourcePrefix}-identity-${resourceToken}'
  location: location
  tags: tags
}

// Azure OpenAI Service
resource cognitiveServices 'Microsoft.CognitiveServices/accounts@2024-10-01' = {
  name: '${resourcePrefix}-openai-${resourceToken}'
  location: location
  tags: tags
  kind: 'OpenAI'
  sku: {
    name: 'S0'
  }
  properties: {
    customSubDomainName: '${resourcePrefix}-openai-${resourceToken}'
    publicNetworkAccess: 'Enabled'
    networkAcls: {
      defaultAction: 'Allow'
    }
  }
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${managedIdentity.id}': {}
    }
  }
}

// OpenAI Model Deployment
resource openAIModelDeployment 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = {
  parent: cognitiveServices
  name: openAIModelName
  properties: {
    model: {
      format: 'OpenAI'
      name: openAIModelName
    }
    versionUpgradeOption: 'OnceNewDefaultVersionAvailable'
    currentCapacity: 450 // Standard deployment capacity - adjust based on your needs

  }
  sku: {
    name: 'Standard'
    capacity: 450
  }
}

// Key Vault for storing secrets
resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: 'kv${resourceToken}'
  location: location
  tags: tags
  properties: {
    tenantId: subscription().tenantId
    sku: {
      family: 'A'
      name: 'standard'
    }
    accessPolicies: [
      {
        tenantId: subscription().tenantId
        objectId: managedIdentity.properties.principalId
        permissions: {
          secrets: ['get', 'list']
        }
      }
    ]
    enableRbacAuthorization: false
    publicNetworkAccess: 'Enabled'
    networkAcls: {
      defaultAction: 'Allow'
      bypass: 'AzureServices'
    }
  }
}

// Store Azure OpenAI secrets in Key Vault (only the API key, not the endpoint)
resource openAIKeySecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: 'AZURE-OPENAI-API-KEY'
  properties: {
    value: cognitiveServices.listKeys().key1
  }
}

// App Service Plan
resource appServicePlan 'Microsoft.Web/serverfarms@2024-04-01' = {
  name: '${resourcePrefix}-plan-${resourceToken}'
  location: location
  tags: tags
  sku: {
    name: 'P1V3'
    tier: 'PremiumV3'
    capacity: 1
  }
  kind: 'linux'
  properties: {
    reserved: true // Required for Linux app service plans
  }
}

// App Service
resource appService 'Microsoft.Web/sites@2024-04-01' = {
  name: '${resourcePrefix}-app-${resourceToken}'
  location: location
  tags: union(tags, { 'azd-service-name': 'web' })
  kind: 'app,linux'
  properties: {
    serverFarmId: appServicePlan.id
    reserved: true
    httpsOnly: true
    siteConfig: {
      linuxFxVersion: 'PYTHON|3.10'
      alwaysOn: true
      ftpsState: 'FtpsOnly'
      minTlsVersion: '1.2'
      appCommandLine: 'chainlit run main.py --host 0.0.0.0 --port 8000'
      appSettings: [
        {
          name: 'AZURE_OPENAI_ENDPOINT'
          value: cognitiveServices.properties.endpoint
        }
        {
          name: 'AZURE_OPENAI_API_KEY'
          value: '@Microsoft.KeyVault(VaultName=${keyVault.name};SecretName=AZURE-OPENAI-API-KEY)'
        }
        {
          name: 'AZURE_OPENAI_MODEL'
          value: openAIModelName
        }
        {
          name: 'OPENAI_API_VERSION'
          value: openAIAPIVersion
        }
        {
          name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
          value: applicationInsights.properties.ConnectionString
        }
        {
          name: 'SCM_DO_BUILD_DURING_DEPLOYMENT'
          value: 'true'
        }
        {
          name: 'ENABLE_ORYX_BUILD'
          value: 'true'
        }
        {
          name: 'POST_BUILD_COMMAND'
          value: 'pip install -r requirements.txt'
        }
        {
          name: 'WEBSITES_PORT'
          value: '8000'
        }
      ]
      cors: {
        allowedOrigins: ['*']
        supportCredentials: false
      }
    }
  }
  identity: {
    type: 'SystemAssigned, UserAssigned'
    userAssignedIdentities: {
      '${managedIdentity.id}': {}
    }
  }
}

// Diagnostic settings for App Service
resource appServiceDiagnosticSettings 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'appservice-diagnostics'
  scope: appService
  properties: {
    workspaceId: logAnalyticsWorkspace.id
    logs: [
      {
        category: 'AppServiceHTTPLogs'
        enabled: true
      }
      {
        category: 'AppServiceConsoleLogs'
        enabled: true
      }
      {
        category: 'AppServiceAppLogs'
        enabled: true
      }
    ]
    metrics: [
      {
        category: 'AllMetrics'
        enabled: true
      }
    ]
  }
}

// Add Key Vault access policy for App Service system-assigned managed identity
resource keyVaultAccessPolicyForAppService 'Microsoft.KeyVault/vaults/accessPolicies@2023-07-01' = {
  parent: keyVault
  name: 'add'
  properties: {
    accessPolicies: [
      {
        tenantId: subscription().tenantId
        objectId: appService.identity.principalId
        permissions: {
          secrets: ['get', 'list']
        }
      }
    ]
  }
}

// Outputs
output APPLICATIONINSIGHTS_CONNECTION_STRING string = applicationInsights.properties.ConnectionString
output AZURE_OPENAI_ENDPOINT string = cognitiveServices.properties.endpoint
output AZURE_OPENAI_MODEL string = openAIModelDeployment.name
output OPENAI_API_VERSION string = openAIAPIVersion
output AZURE_OPENAI_API_KEY string = cognitiveServices.listKeys().key1
// Note: AZURE_OPENAI_API_KEY is securely stored in Key Vault and accessed via App Service configuration

output SERVICE_WEB_NAME string = appService.name
output SERVICE_WEB_URI string = 'https://${appService.properties.defaultHostName}'
