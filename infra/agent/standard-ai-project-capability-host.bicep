// Wires up the account- and project-level capability hosts for the AI Foundry
// Agents service. The project-level capabilityHost requires all three of
// vectorStoreConnections, storageConnections, and threadStorageConnections.

param cosmosDbConnection string
param azureStorageConnection string
param aiSearchConnection string
param projectName string
param aiServicesAccountName string
param projectCapHost string
param accountCapHost string

var threadConnections = ['${cosmosDbConnection}']
var storageConnections = ['${azureStorageConnection}']
var vectorStoreConnections = ['${aiSearchConnection}']

resource account 'Microsoft.CognitiveServices/accounts@2025-04-01-preview' existing = {
   name: aiServicesAccountName
}

resource project 'Microsoft.CognitiveServices/accounts/projects@2025-04-01-preview' existing = {
  name: projectName
  parent: account
}

resource accountCapabilityHost 'Microsoft.CognitiveServices/accounts/capabilityHosts@2025-04-01-preview' = {
   name: accountCapHost
   parent: account
   properties: {
     capabilityHostKind: 'Agents'
   }
}

resource projectCapabilityHost 'Microsoft.CognitiveServices/accounts/projects/capabilityHosts@2025-04-01-preview' = {
  name: projectCapHost
  parent: project
  properties: {
    #disable-next-line BCP037
    capabilityHostKind: 'Agents'
    vectorStoreConnections: vectorStoreConnections
    storageConnections: storageConnections
    threadStorageConnections: threadConnections
  }
  dependsOn: [
    accountCapabilityHost
  ]
}
