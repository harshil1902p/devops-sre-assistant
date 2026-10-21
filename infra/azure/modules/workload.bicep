// Workload resources (resource-group scope). Phase 4 deploys only Azure OpenAI; Phase 7 enables the platform.
param location string
param prefix string
param suffix string
param tags object
param principalId string
param principalType string
param chatModel object
param embeddingModel object
param deployPlatform bool
param deployApps bool
param imageTag string
param githubRepo string

// Built-in role definition ids (stable GUIDs)
var roles = {
  openAiUser: '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd' // Cognitive Services OpenAI User
}

// ---------------------------------------------------------------- Azure OpenAI
resource openai 'Microsoft.CognitiveServices/accounts@2024-10-01' = {
  name: 'aoai-${prefix}-${suffix}'
  location: location
  tags: tags
  kind: 'OpenAI'
  sku: {
    name: 'S0'
  }
  properties: {
    customSubDomainName: 'aoai-${prefix}-${suffix}' // required for Entra ID (token) auth
    disableLocalAuth: true // no API keys at all: Entra ID + RBAC only
    publicNetworkAccess: 'Enabled' // private endpoints would be used in a real bank VNet
  }
}

resource chat 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = {
  parent: openai
  name: chatModel.name
  sku: {
    name: 'GlobalStandard'
    capacity: chatModel.capacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: chatModel.name
      version: chatModel.version
    }
    versionUpgradeOption: 'NoAutoUpgrade' // pin versions; evals gate upgrades
  }
}

resource embedding 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = {
  parent: openai
  name: embeddingModel.name
  dependsOn: [chat] // deployments on one account must be created sequentially
  sku: {
    name: 'GlobalStandard'
    capacity: embeddingModel.capacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: embeddingModel.name
      version: embeddingModel.version
    }
    versionUpgradeOption: 'NoAutoUpgrade'
  }
}

resource openAiUserForPrincipal 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: openai
  name: guid(openai.id, principalId, roles.openAiUser)
  properties: {
    principalId: principalId
    principalType: principalType
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.openAiUser)
  }
}

// ----------------------------------------------------------- Phase 7 platform
module platform 'platform.bicep' = if (deployPlatform) {
  name: 'platform'
  params: {
    location: location
    prefix: prefix
    suffix: suffix
    tags: tags
    principalId: principalId
    principalType: principalType
    openAiName: openai.name
    chatDeployment: chatModel.name
    embeddingDeployment: embeddingModel.name
    deployApps: deployApps
    imageTag: imageTag
    githubRepo: githubRepo
  }
}

output openAiEndpoint string = openai.properties.endpoint
output searchEndpoint string = deployPlatform ? platform!.outputs.searchEndpoint : ''
output logAnalyticsWorkspaceId string = deployPlatform ? platform!.outputs.logAnalyticsWorkspaceId : ''
output registryLoginServer string = deployPlatform ? platform!.outputs.registryLoginServer : ''
output agentApiUrl string = deployPlatform ? platform!.outputs.agentApiUrl : ''
output logsIngestion object = deployPlatform ? platform!.outputs.logsIngestion : {}
output keyVaultUri string = deployPlatform ? platform!.outputs.keyVaultUri : ''
output agentIdentityClientId string = deployPlatform ? platform!.outputs.agentIdentityClientId : ''
