using 'main.bicep'

// Non-sensitive defaults only. principalId and alertEmail are passed on the command line at deploy time.
param location = 'canadaeast'
param prefix = 'sre'
param budgetAmount = 1
param principalId = ''
param alertEmail = ''
param deployPlatform = true // Phase 7
param githubRepo = 'harshil1902p/devops-sre-assistant'
param deployApps = false
