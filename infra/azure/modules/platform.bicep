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

@description('Phase 8: container registry (Basic, ~$0.17/day). Needed before images can be pushed')
param deployRegistry bool = false

@description('Phase 8: Entra app (client) id or app id URI the API accepts tokens for')
param apiAudience string = ''

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
  acrPull: '7f951dda-4ed3-4680-a7ca-43fe172d538d'
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

// =================================================================== Phase 8
var withRegistry = deployRegistry || deployApps

resource acr 'Microsoft.ContainerRegistry/registries@2023-07-01' = if (withRegistry) {
  name: 'acr${prefix}${suffix}'
  location: location
  tags: tags
  sku: {
    name: 'Basic'
  }
  properties: {
    adminUserEnabled: false // pulls use the managed identity (AcrPull), never a registry password
  }
}

resource agentAcrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (withRegistry) {
  scope: acr
  name: guid(acr.id, roles.acrPull, 'agent')
  properties: {
    principalId: agentIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.acrPull)
  }
}

resource appi 'Microsoft.Insights/components@2020-02-02' = if (deployApps) {
  name: 'appi-${prefix}-${suffix}'
  location: location
  tags: tags
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: law.id // same workspace (and the same 0.1 GB/day cap)
    IngestionMode: 'LogAnalytics'
    DisableLocalAuth: true // telemetry must be sent with an Entra token (managed identity)
  }
}

resource agentAppiPublisher 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (deployApps) {
  scope: appi
  name: guid(appi.id, roles.monitoringMetricsPublisher, 'agent')
  properties: {
    principalId: agentIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.monitoringMetricsPublisher)
  }
}

resource cae 'Microsoft.App/managedEnvironments@2024-03-01' = if (deployApps) {
  name: 'cae-${prefix}-${suffix}'
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: law.properties.customerId
        sharedKey: law.listKeys().primarySharedKey // resolved inside ARM at deploy time; never in the repo or outputs
      }
    }
    workloadProfiles: [
      {
        name: 'Consumption'
        workloadProfileType: 'Consumption'
      }
    ]
  }
}

var apiEnv = [
  { name: 'AZURE_CLIENT_ID', value: agentIdentity.properties.clientId } // DefaultAzureCredential -> this identity
  { name: 'SRE_LLM_PROVIDER', value: 'azure_openai' }
  { name: 'SRE_CHAT_MODEL', value: chatDeployment }
  { name: 'SRE_EMBEDDING_MODEL', value: embeddingDeployment }
  { name: 'SRE_AZURE_OPENAI_ENDPOINT', value: openai.properties.endpoint }
  { name: 'SRE_RETRIEVER_BACKEND', value: 'azure_search' }
  { name: 'SRE_AZURE_SEARCH_ENDPOINT', value: 'https://${search.name}.search.windows.net' }
  { name: 'SRE_AZURE_SEARCH_RERANK', value: semanticSearch == 'free' ? 'semantic' : 'none' }
  { name: 'SRE_RERANKER_MODEL', value: '' } // no local ONNX model in the cloud image path
  { name: 'SRE_TELEMETRY_BACKEND', value: 'azure_monitor' }
  { name: 'SRE_LOG_ANALYTICS_WORKSPACE_ID', value: law.properties.customerId }
  { name: 'SRE_KEY_VAULT_URL', value: kv.properties.vaultUri }
  { name: 'SRE_ENTRA_TENANT_ID', value: subscription().tenantId }
  { name: 'SRE_API_AUDIENCE', value: apiAudience }
  { name: 'SRE_APPINSIGHTS_CONNECTION_STRING', value: deployApps ? appi!.properties.ConnectionString : '' }
  { name: 'SRE_SPEND_LIMIT_USD', value: '0.30' } // per replica lifetime; Azure budget + TPM quota are the global limits
  { name: 'SRE_API_RATE_PER_MINUTE', value: '4' }
]

resource api 'Microsoft.App/containerApps@2024-03-01' = if (deployApps) {
  name: 'ca-${prefix}-agent-api'
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${agentIdentity.id}': {}
    }
  }
  properties: {
    managedEnvironmentId: cae.id
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
        allowInsecure: false // HTTPS only (managed certificate on *.azurecontainerapps.io)
      }
      registries: [
        {
          server: acr!.properties.loginServer
          identity: agentIdentity.id
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'api'
          image: '${acr!.properties.loginServer}/sre-agent-api:${imageTag}'
          resources: {
            cpu: json('1.0')
            memory: '2Gi'
          }
          env: apiEnv
          probes: [
            {
              type: 'Startup' // spaCy + MCP subprocess + tool discovery take a while on a cold start
              httpGet: { path: '/readyz', port: 8000 }
              periodSeconds: 5
              failureThreshold: 36
            }
            {
              type: 'Liveness'
              httpGet: { path: '/healthz', port: 8000 }
              periodSeconds: 30
            }
            {
              type: 'Readiness'
              httpGet: { path: '/readyz', port: 8000 }
              periodSeconds: 10
            }
          ]
        }
      ]
      scale: {
        minReplicas: 0 // scale to zero: no requests, no compute bill
        maxReplicas: 1 // in-memory threads/approvals + per-replica rate limit (see docs/learning/08-production.md)
        rules: [
          {
            name: 'http'
            http: {
              metadata: {
                concurrentRequests: '10'
              }
            }
          }
        ]
      }
    }
  }
  dependsOn: [agentAcrPull, agentAppiPublisher, kvRoles, searchRoles, lawRoles, agentOpenAiUser]
}

output searchEndpoint string = 'https://${search.name}.search.windows.net'
output logAnalyticsWorkspaceId string = law.properties.customerId // the GUID the query API wants
output keyVaultUri string = kv.properties.vaultUri
output agentIdentityClientId string = agentIdentity.properties.clientId
output logsIngestion object = {
  endpoint: dcr.properties.endpoints.logsIngestion
  ruleId: dcr.properties.immutableId
}
output registryLoginServer string = withRegistry ? acr!.properties.loginServer : ''
output agentApiUrl string = deployApps ? 'https://${api!.properties.configuration.ingress.fqdn}' : ''
