// Phase 7 platform (all free tier or pay-per-use with caps):
//   AI Search (free), Log Analytics (+ 0.1 GB/day cap) with custom tables and a Direct DCR (log shipping),
//   Key Vault (RBAC), and a user-assigned managed identity that GitHub Actions can federate into (OIDC, no secrets).
// Phase 8 adds the container registry and Container Apps (the only resources with an hourly price).
param location string
param prefix string
param suffix string
param tags object
param principalId string
param principalType string
param openAiName string
param chatDeployment string
param embeddingDeployment string
param deployApps bool
param imageTag string

@description('GitHub repo (owner/name) allowed to sign in as the agent identity via OIDC')
param githubRepo string

@description('GitHub environment the federated credential is bound to (protect it with required reviewers)')
param githubEnvironment string = 'cloud-eval'

@description('''Exact OIDC subject GitHub presents. Newer repos use immutable ids: repo:OWNER@OWNER_ID/REPO@REPO_ID:...
(defends against a deleted repo/owner name being re-registered). Empty = legacy name-based subject.''')
param githubOidcSubject string = ''

@description('Semantic ranker plan: free = 1,000 queries/month at no cost')
@allowed(['disabled', 'free'])
param semanticSearch string = 'free'

var roles = {
  openAiUser: '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'
  searchIndexDataContributor: '8ebe5a00-799e-43f5-93ac-243d3dce84a7'
  searchIndexDataReader: '1407120a-92aa-4202-b7e9-c0e197c71c8f'
  searchServiceContributor: '7ca78c08-252a-4471-8644-bb5ff32d4ba0'
  logAnalyticsReader: '73c42c96-874c-492b-b04d-ab87d138a893'
  monitoringMetricsPublisher: '3913510d-42f4-4e42-8a64-420c390055eb' // also grants Logs Ingestion API upload
  keyVaultSecretsOfficer: 'b86a8fe4-44ce-4948-aee5-eccb2c155cd7' // public built-in role id gitleaks:allow
  keyVaultSecretsUser: '4633458b-17de-408a-b874-0445c86b69e6' // public built-in role id gitleaks:allow
}

resource openai 'Microsoft.CognitiveServices/accounts@2024-10-01' existing = {
  name: openAiName
}

// ------------------------------------------------------------------ identity
resource agentIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-${prefix}-agent'
  location: location
  tags: tags
}

resource githubFederation 'Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials@2023-01-31' = {
  parent: agentIdentity
  name: 'github-${githubEnvironment}'
  properties: {
    issuer: 'https://token.actions.githubusercontent.com'
    subject: empty(githubOidcSubject) ? 'repo:${githubRepo}:environment:${githubEnvironment}' : githubOidcSubject
    audiences: ['api://AzureADTokenExchange']
  }
}

// ---------------------------------------------------------------- AI Search
resource search 'Microsoft.Search/searchServices@2023-11-01' = {
  name: 'srch-${prefix}-${suffix}'
  location: location
  tags: tags
  sku: {
    name: 'free' // 50 MB, 3 indexes; one free service per subscription
  }
  properties: {
    replicaCount: 1
    partitionCount: 1
    hostingMode: 'default'
    disableLocalAuth: true // no admin/query keys: Entra ID + RBAC only
    semanticSearch: semanticSearch
    publicNetworkAccess: 'enabled'
  }
}

// ------------------------------------------------------- Log Analytics + DCR
resource law 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'log-${prefix}-${suffix}'
  location: location
  tags: tags
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
    workspaceCapping: {
      dailyQuotaGb: json('0.1') // hard ingestion cap: a runaway shipper can't run up a bill
    }
  }
}

var tableSchemas = {
  SreServiceLogs_CL: [
    { name: 'TimeGenerated', type: 'datetime' }
    { name: 'Service', type: 'string' }
    { name: 'Environment', type: 'string' }
    { name: 'Container', type: 'string' }
    { name: 'Line', type: 'string' }
  ]
  SreServiceEvents_CL: [
    { name: 'TimeGenerated', type: 'datetime' }
    { name: 'Service', type: 'string' }
    { name: 'Action', type: 'string' }
    { name: 'Attributes', type: 'dynamic' }
  ]
  SreServiceHealth_CL: [
    { name: 'TimeGenerated', type: 'datetime' }
    { name: 'Service', type: 'string' }
    { name: 'Environment', type: 'string' }
    { name: 'Container', type: 'string' }
    { name: 'Status', type: 'string' }
    { name: 'Health', type: 'string' }
    { name: 'RestartCount', type: 'int' }
    { name: 'Image', type: 'string' }
    { name: 'StartedAt', type: 'string' }
    { name: 'OomKilled', type: 'boolean' }
    { name: 'ExitCode', type: 'int' }
    { name: 'FailingStreak', type: 'int' }
    { name: 'Probes', type: 'dynamic' }
    { name: 'Ports', type: 'dynamic' }
    { name: 'CpuPercent', type: 'real' }
    { name: 'MemoryUsageMb', type: 'real' }
    { name: 'MemoryLimitMb', type: 'real' }
  ]
}

resource tables 'Microsoft.OperationalInsights/workspaces/tables@2022-10-01' = [
  for t in items(tableSchemas): {
    parent: law
    name: t.key
    properties: {
      plan: 'Analytics'
      retentionInDays: 30
      schema: {
        name: t.key
        columns: t.value
      }
    }
  }
]

// Direct DCR: has its own logs-ingestion endpoint, no separate DCE needed.
resource dcr 'Microsoft.Insights/dataCollectionRules@2023-03-11' = {
  name: 'dcr-${prefix}-telemetry'
  location: location
  tags: tags
  kind: 'Direct'
  properties: {
    streamDeclarations: toObject(
      items(tableSchemas),
      t => 'Custom-${replace(t.key, '_CL', '')}',
      t => { columns: t.value }
    )
    destinations: {
      logAnalytics: [
        {
          name: 'law'
          workspaceResourceId: law.id
        }
      ]
    }
    dataFlows: [
      for t in items(tableSchemas): {
        streams: ['Custom-${replace(t.key, '_CL', '')}']
        destinations: ['law']
        transformKql: 'source'
        outputStream: 'Custom-${t.key}'
      }
    ]
  }
  dependsOn: [tables]
}

// ----------------------------------------------------------------- Key Vault
resource kv 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: 'kv-${prefix}-${suffix}'
  location: location
  tags: tags
  properties: {
    tenantId: subscription().tenantId
    sku: {
      family: 'A'
      name: 'standard'
    }
    enableRbacAuthorization: true // no access policies: same RBAC model as everything else
    softDeleteRetentionInDays: 7
    publicNetworkAccess: 'Enabled'
  }
}

// --------------------------------------------------------------------- RBAC
// Human/CI principal: build the index, ship logs, query logs, manage secrets.
// Agent identity: read-only (query index, query logs, read secrets, call the model).
var assignments = [
  { scope: 'search', role: roles.searchIndexDataContributor, who: 'human' }
  { scope: 'search', role: roles.searchServiceContributor, who: 'human' }
  { scope: 'search', role: roles.searchIndexDataReader, who: 'agent' }
  { scope: 'law', role: roles.logAnalyticsReader, who: 'human' }
  { scope: 'law', role: roles.logAnalyticsReader, who: 'agent' }
  { scope: 'dcr', role: roles.monitoringMetricsPublisher, who: 'human' }
  { scope: 'kv', role: roles.keyVaultSecretsOfficer, who: 'human' }
  { scope: 'kv', role: roles.keyVaultSecretsUser, who: 'agent' }
]

resource searchRoles 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for a in filter(assignments, x => x.scope == 'search'): {
    scope: search
    name: guid(search.id, a.role, a.who)
    properties: {
      principalId: a.who == 'human' ? principalId : agentIdentity.properties.principalId
      principalType: a.who == 'human' ? principalType : 'ServicePrincipal'
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', a.role)
    }
  }
]

resource lawRoles 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for a in filter(assignments, x => x.scope == 'law'): {
    scope: law
    name: guid(law.id, a.role, a.who)
    properties: {
      principalId: a.who == 'human' ? principalId : agentIdentity.properties.principalId
      principalType: a.who == 'human' ? principalType : 'ServicePrincipal'
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', a.role)
    }
  }
]

resource dcrRoles 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for a in filter(assignments, x => x.scope == 'dcr'): {
    scope: dcr
    name: guid(dcr.id, a.role, a.who)
    properties: {
      principalId: a.who == 'human' ? principalId : agentIdentity.properties.principalId
      principalType: a.who == 'human' ? principalType : 'ServicePrincipal'
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', a.role)
    }
  }
]

resource kvRoles 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for a in filter(assignments, x => x.scope == 'kv'): {
    scope: kv
    name: guid(kv.id, a.role, a.who)
    properties: {
      principalId: a.who == 'human' ? principalId : agentIdentity.properties.principalId
      principalType: a.who == 'human' ? principalType : 'ServicePrincipal'
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', a.role)
    }
  }
]

resource agentOpenAiUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: openai
  name: guid(openai.id, roles.openAiUser, 'agent')
  properties: {
    principalId: agentIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.openAiUser)
  }
}

output searchEndpoint string = 'https://${search.name}.search.windows.net'
output logAnalyticsWorkspaceId string = law.properties.customerId // the GUID the query API wants
output keyVaultUri string = kv.properties.vaultUri
output agentIdentityClientId string = agentIdentity.properties.clientId
output logsIngestion object = {
  endpoint: dcr.properties.endpoints.logsIngestion
  ruleId: dcr.properties.immutableId
}
// Phase 8 (Container Apps + registry)
output registryLoginServer string = ''
output agentApiUrl string = ''
