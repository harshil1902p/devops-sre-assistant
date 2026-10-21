// Subscription-scope entry point: resource group, budget alert, and the workload module.
//   az deployment sub create -l canadaeast -f infra/azure/main.bicep -p infra/azure/main.bicepparam \
//     -p principalId=$(az ad signed-in-user show --query id -o tsv) alertEmail=<you@example.com>
targetScope = 'subscription'

@description('Azure region for all resources')
param location string = 'canadaeast'

@description('Short prefix for resource names')
@maxLength(8)
param prefix string = 'sre'

@description('Object id of the human/CI principal that gets data-plane access (keyless, RBAC)')
param principalId string

@description('Type of principalId')
@allowed(['User', 'ServicePrincipal', 'Group'])
param principalType string = 'User'

@description('Email for budget alerts (passed at deploy time, never committed)')
param alertEmail string

@description('Monthly budget (USD) that triggers alert emails. Alerts only; Azure budgets never stop spending.')
param budgetAmount int = 1

@description('First day of the current month, required by the budget API')
param budgetStartDate string = '${utcNow('yyyy-MM')}-01'

@description('Chat model deployment')
param chatModel object = {
  name: 'gpt-4.1-mini'
  version: '2025-04-14'
  capacity: 150 // thousands of tokens per minute (TPM); quota is 200k. Raised from 50 after eval runs hit 429s
}

@description('Embedding model deployment')
param embeddingModel object = {
  name: 'text-embedding-3-small'
  version: '1'
  capacity: 50
}

@description('Phase 7+: deploy AI Search, Log Analytics, Key Vault, Container Apps, registry')
param deployPlatform bool = false

@description('Phase 7+: deploy the container apps (requires images pushed to the registry)')
param deployApps bool = false

param imageTag string = 'latest'

@description('GitHub repo (owner/name) allowed to federate into the agent identity (OIDC)')
param githubRepo string = ''

var suffix = take(uniqueString(subscription().id, prefix, location), 6)
var tags = {
  project: 'devops-sre-assistant'
  managedBy: 'bicep'
}

resource rg 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: 'rg-${prefix}-assistant'
  location: location
  tags: tags
}

resource budget 'Microsoft.Consumption/budgets@2023-11-01' = {
  name: 'budget-${prefix}-assistant'
  properties: {
    category: 'Cost'
    amount: budgetAmount
    timeGrain: 'Monthly'
    timePeriod: {
      startDate: budgetStartDate
    }
    notifications: {
      actual50: {
        enabled: true
        operator: 'GreaterThanOrEqualTo'
        threshold: 50
        thresholdType: 'Actual'
        contactEmails: [alertEmail]
      }
      actual100: {
        enabled: true
        operator: 'GreaterThanOrEqualTo'
        threshold: 100
        thresholdType: 'Actual'
        contactEmails: [alertEmail]
      }
      forecast100: {
        enabled: true
        operator: 'GreaterThanOrEqualTo'
        threshold: 100
        thresholdType: 'Forecasted'
        contactEmails: [alertEmail]
      }
    }
  }
}

module workload 'modules/workload.bicep' = {
  scope: rg
  name: 'workload'
  params: {
    location: location
    prefix: prefix
    suffix: suffix
    tags: tags
    principalId: principalId
    principalType: principalType
    chatModel: chatModel
    embeddingModel: embeddingModel
    deployPlatform: deployPlatform
    deployApps: deployApps
    imageTag: imageTag
    githubRepo: githubRepo
  }
}

output resourceGroup string = rg.name
output openAiEndpoint string = workload.outputs.openAiEndpoint
output chatDeployment string = chatModel.name
output embeddingDeployment string = embeddingModel.name
output searchEndpoint string = workload.outputs.searchEndpoint
output logAnalyticsWorkspaceId string = workload.outputs.logAnalyticsWorkspaceId
output registryLoginServer string = workload.outputs.registryLoginServer
output agentApiUrl string = workload.outputs.agentApiUrl
output logsIngestion object = workload.outputs.logsIngestion
output keyVaultUri string = workload.outputs.keyVaultUri
output agentIdentityClientId string = workload.outputs.agentIdentityClientId
