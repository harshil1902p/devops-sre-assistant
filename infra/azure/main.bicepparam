using 'main.bicep'

// Non-sensitive defaults only. principalId and alertEmail are passed on the command line at deploy time.
param location = 'canadaeast'
param prefix = 'sre'
param budgetAmount = 1
param principalId = ''
param alertEmail = ''
param deployPlatform = false
param deployApps = false
