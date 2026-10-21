using 'main.bicep'

// Non-sensitive defaults only. principalId and alertEmail are passed on the command line at deploy time.
param location = 'canadaeast'
param prefix = 'sre'
param budgetAmount = 1
param principalId = ''
param alertEmail = ''
param deployPlatform = true // Phase 7
param githubRepo = 'harshil1902p/devops-sre-assistant'
// Public ids (owner id / repo id), as presented by GitHub in the token's `sub` claim:
param githubOidcSubject = 'repo:harshil1902p@336735813/devops-sre-assistant@1427612424:environment:cloud-eval'
param deployApps = false
