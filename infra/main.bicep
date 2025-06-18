// Main Bicep template for azd-mcp-client deployment to Azure US Government
targetScope = 'subscription'

// Parameters
@minLength(1)
@maxLength(64)
@description('Name of the environment used for resource naming')
param environmentName string

@minLength(1)
@description('Primary location for all resources')
param location string

@minLength(1)
@description('Name of the resource group')
param resourceGroupName string

// Variables
var resourceToken = toLower(uniqueString(subscription().id, environmentName))
var resourcePrefix = 'azd-mcp-client'

// Tags for all resources (without service-specific tags)
var tags = {
  'azd-env-name': environmentName
}

// Create resource group with azd environment name
resource resourceGroup 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: resourceGroupName
  location: location
  tags: tags
}

// Deploy resources to the resource group
module resources 'resources.bicep' = {
  name: 'resources'
  scope: resourceGroup
  params: {
    location: location
    resourceToken: resourceToken
    resourcePrefix: resourcePrefix
    tags: tags
  }
}

// Outputs
output AZURE_LOCATION string = location
output AZURE_TENANT_ID string = subscription().tenantId
output AZURE_SUBSCRIPTION_ID string = subscription().subscriptionId
output RESOURCE_GROUP_ID string = resourceGroup.id

output APPLICATIONINSIGHTS_CONNECTION_STRING string = resources.outputs.APPLICATIONINSIGHTS_CONNECTION_STRING
output AZURE_OPENAI_ENDPOINT string = resources.outputs.AZURE_OPENAI_ENDPOINT
output AZURE_OPENAI_MODEL string = resources.outputs.AZURE_OPENAI_MODEL
output OPENAI_API_VERSION string = resources.outputs.OPENAI_API_VERSION
// Note: AZURE_OPENAI_API_KEY is securely stored in Key Vault and accessed via App Service configuration

output SERVICE_WEB_NAME string = resources.outputs.SERVICE_WEB_NAME
output SERVICE_WEB_URI string = resources.outputs.SERVICE_WEB_URI
