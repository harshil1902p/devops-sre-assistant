// Placeholder replaced in Phase 7 (AI Search, Log Analytics, Key Vault, registry, Container Apps).
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

output searchEndpoint string = ''
output logAnalyticsWorkspaceId string = ''
output registryLoginServer string = ''
output agentApiUrl string = ''
output logsIngestion object = {}
