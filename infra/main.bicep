targetScope = 'resourceGroup'

@description('Azure region for all resources.')
param location string = resourceGroup().location

@description('App Service app name. Must be globally unique.')
param appName string = 'chatbubble-${uniqueString(resourceGroup().id)}'

@description('Weather MCP Function App name. Must be globally unique.')
param weatherFunctionAppName string = '${appName}-func'

@description('App Service plan name.')
param appServicePlanName string = '${appName}-plan'

@description('Weather MCP Function App Service plan name used when SKU is F1.')
param weatherFunctionAppServicePlanName string = '${weatherFunctionAppName}-plan'

@description('App Service plan SKU. Allowed values: F1 (Free) or B1/B3 (Basic).')
@allowed([
  'F1'
  'B1'
  'B3'
])
param appServiceSkuName string 

@description('User Assigned Managed Identity name for GitHub Actions deployments.')
param githubDeployIdentityName string = '${appName}-gha-mi'

@description('GitHub organization or user name that owns the repository.')
param githubOrg string = ''

@description('GitHub repository name for workload identity federation.')
param githubRepo string = ''

@description('GitHub branch allowed to request OIDC tokens for deployment.')
param githubBranch string = 'main'

@description('Nominatim base URL used by the Weather MCP Function App for forward geocoding requests.')
param nominatimBaseUrl string = 'https://nominatim.openstreetmap.org/search'

@description('User-Agent header used when calling Nominatim.')
param nominatimUserAgent string = 'simple-chat-bubble-weather-mcp/1.0'

@description('User-Agent header used when calling weather.gov.')
param nwsUserAgent string = 'simple-chat-bubble-weather-mcp/1.0 (contact: admin@example.com)'

@description('Azure Communication Services resource name.')
param communicationServiceName string = '${appName}-acs'

@description('Data location for Azure Communication Services (for example: United States or Europe).')
param communicationServiceDataLocation string = 'United States'

@description('Azure Communication Email Service name.')
param communicationEmailServiceName string = '${appName}-email'

@description('Azure-managed email domain resource name. Azure requires this to be AzureManagedDomain.')
param communicationEmailDomainName string = 'AzureManagedDomain'

@description('Sender local-part used to construct sender address for email (for example: DoNotReply).')
param communicationEmailSenderLocalPart string = 'DoNotReply'

var enableGithubFederation = !empty(githubOrg) && !empty(githubRepo)
var isFreeSku = appServiceSkuName == 'F1'
var appServiceSkuTier = appServiceSkuName == 'F1' ? 'Free' : 'Basic'
var webAppUrl = 'https://${appName}.azurewebsites.net'
var weatherFunctionAppUrl = 'https://${weatherFunctionAppName}.azurewebsites.net'
var weatherFunctionStorageAccountName = 'st${uniqueString(resourceGroup().id, weatherFunctionAppName)}'
var communicationServiceEndpoint = 'https://${communicationServiceName}.communication.azure.com'
var communicationEmailSenderAddress = '${communicationEmailSenderLocalPart}@${emailDomain.properties.fromSenderDomain}'

resource appServicePlan 'Microsoft.Web/serverfarms@2024-04-01' = {
  name: appServicePlanName
  location: location
  kind: 'linux'
  sku: {
    name: appServiceSkuName
    tier: appServiceSkuTier
    size: appServiceSkuName
    capacity: 1
  }
  properties: {
    reserved: true
  }
}

resource weatherFunctionAppServicePlan 'Microsoft.Web/serverfarms@2024-04-01' = if (isFreeSku) {
  name: weatherFunctionAppServicePlanName
  location: location
  kind: 'linux'
  sku: {
    name: appServiceSkuName
    tier: appServiceSkuTier
    size: appServiceSkuName
    capacity: 1
  }
  properties: {
    reserved: true
  }
}

resource weatherFunctionStorage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: weatherFunctionStorageAccountName
  location: location
  kind: 'StorageV2'
  sku: {
    name: 'Standard_LRS'
  }
  properties: {
    supportsHttpsTrafficOnly: true
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
  }
}

resource communicationService 'Microsoft.Communication/communicationServices@2025-09-01' = {
  name: communicationServiceName
  location: 'global'
  properties: {
    dataLocation: communicationServiceDataLocation
    linkedDomains: [
      emailDomain.id
    ]
  }
}

resource emailService 'Microsoft.Communication/emailServices@2025-09-01' = {
  name: communicationEmailServiceName
  location: 'global'
  properties: {
    dataLocation: communicationServiceDataLocation
  }
}

resource emailDomain 'Microsoft.Communication/emailServices/domains@2025-09-01' = {
  parent: emailService
  name: communicationEmailDomainName
  location: 'global'
  properties: {
    domainManagement: 'AzureManaged'
    userEngagementTracking: 'Disabled'
  }
}

resource githubDeployIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: githubDeployIdentityName
  location: location
}

resource githubOidcFederatedCredential 'Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials@2023-01-31' = if (enableGithubFederation) {
  parent: githubDeployIdentity
  name: 'github-main'
  properties: {
    issuer: 'https://token.actions.githubusercontent.com'
    audiences: [
      'api://AzureADTokenExchange'
    ]
    subject: 'repo:${githubOrg}/${githubRepo}:ref:refs/heads/${githubBranch}'
  }
}

resource webApp 'Microsoft.Web/sites@2024-04-01' = {
  name: appName
  location: location
  kind: 'app,linux'
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    serverFarmId: appServicePlan.id
    siteConfig: {
      linuxFxVersion: 'PYTHON|3.13'
      alwaysOn: false
      appCommandLine: 'gunicorn --worker-class uvicorn.workers.UvicornWorker --bind=0.0.0.0:8000 app.main:app'
      appSettings: [
        {
          name: 'SCM_DO_BUILD_DURING_DEPLOYMENT'
          value: 'true'
        }
        {
          name: 'WEBSITES_PORT'
          value: '8000'
        }
        {
          name: 'AZURE_WEATHER_FUNCTION_APP_URL'
          value: weatherFunctionAppUrl
        }
        {
          name: 'AZURE_WEATHER_WEBAPP_URL'
          value: weatherFunctionAppUrl
        }
      ]
    }
    httpsOnly: true
  }
}

resource weatherFunctionApp 'Microsoft.Web/sites@2024-04-01' = {
  name: weatherFunctionAppName
  location: location
  kind: 'functionapp,linux'
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    serverFarmId: isFreeSku ? weatherFunctionAppServicePlan.id : appServicePlan.id
    siteConfig: {
      linuxFxVersion: 'PYTHON|3.13'
      alwaysOn: false
      cors: {
        allowedOrigins: [
          webAppUrl
          'https://portal.azure.com'
        ]
      }
      appSettings: [
        {
          name: 'FUNCTIONS_WORKER_RUNTIME'
          value: 'python'
        }
        {
          name: 'FUNCTIONS_EXTENSION_VERSION'
          value: '~4'
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
          name: 'AzureWebJobsStorage__accountName'
          value: weatherFunctionStorage.name
        }
        {
          name: 'AzureWebJobsStorage__credential'
          value: 'managedidentity'
        }
        {
          name: 'NOMINATIM_BASE_URL'
          value: nominatimBaseUrl
        }
        {
          name: 'NOMINATIM_USER_AGENT'
          value: nominatimUserAgent
        }
        {
          name: 'NWS_USER_AGENT'
          value: nwsUserAgent
        }
        {
          name: 'AZURE_COMMUNICATION_SERVICE_ENDPOINT'
          value: communicationServiceEndpoint
        }
        {
          name: 'AZURE_COMMUNICATION_SERVICE_RESOURCE_ID'
          value: communicationService.id
        }
        {
          name: 'AZURE_COMMUNICATION_EMAIL_CONNECTION_STRING'
          value: communicationService.listKeys().primaryConnectionString
        }
        {
          name: 'AZURE_COMMUNICATION_EMAIL_SENDER_ADDRESS'
          value: communicationEmailSenderAddress
        }
        {
          name: 'AZURE_COMMUNICATION_EMAIL_DOMAIN'
          value: emailDomain.properties.fromSenderDomain
        }
        {
          name: 'AZURE_COMMUNICATION_EMAIL_SERVICE_NAME'
          value: emailService.name
        }
      ]
    }
    httpsOnly: true
  }
}

output AZURE_WEBAPP_NAME string = webApp.name
output AZURE_WEBAPP_URL string = 'https://${webApp.properties.defaultHostName}'
output AZURE_WEATHER_FUNCTION_APP_NAME string = weatherFunctionApp.name
output AZURE_WEATHER_FUNCTION_APP_URL string = 'https://${weatherFunctionApp.properties.defaultHostName}'
output GITHUB_DEPLOY_MANAGED_IDENTITY_CLIENT_ID string = githubDeployIdentity.properties.clientId
output GITHUB_DEPLOY_MANAGED_IDENTITY_PRINCIPAL_ID string = githubDeployIdentity.properties.principalId
output GITHUB_DEPLOY_MANAGED_IDENTITY_RESOURCE_ID string = githubDeployIdentity.id
output APP_SERVICE_SKU_NAME string = appServiceSkuName
output AZURE_COMMUNICATION_SERVICE_NAME string = communicationService.name
output AZURE_COMMUNICATION_SERVICE_RESOURCE_ID string = communicationService.id
output AZURE_COMMUNICATION_SERVICE_ENDPOINT string = communicationServiceEndpoint
output AZURE_COMMUNICATION_EMAIL_SERVICE_NAME string = emailService.name
output AZURE_COMMUNICATION_EMAIL_DOMAIN string = emailDomain.properties.fromSenderDomain
output AZURE_COMMUNICATION_EMAIL_SENDER_ADDRESS string = communicationEmailSenderAddress
