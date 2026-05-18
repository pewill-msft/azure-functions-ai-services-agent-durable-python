// Role assignments that target containers created by the AI Foundry Agents
// capability host. These must be applied AFTER the capability host has been
// provisioned, since the containers only exist at that point.

param aiProjectPrincipalId string
param aiProjectPrincipalType string = 'ServicePrincipal'
param aiProjectWorkspaceId string

param aiStorageAccountName string
param cosmosDbAccountName string

// Blob containers used by the agent are scoped via an ABAC condition.

resource storage 'Microsoft.Storage/storageAccounts@2022-05-01' existing = {
  name: aiStorageAccountName
}

var storageBlobDataOwnerRoleDefinitionId = 'b7e6dc6d-f1e8-4753-8033-0f276bb0955b'
var conditionStr = '((!(ActionMatches{\'Microsoft.Storage/storageAccounts/blobServices/containers/blobs/tags/read\'})  AND  !(ActionMatches{\'Microsoft.Storage/storageAccounts/blobServices/containers/blobs/filter/action\'}) AND  !(ActionMatches{\'Microsoft.Storage/storageAccounts/blobServices/containers/blobs/tags/write\'}) ) OR (@Resource[Microsoft.Storage/storageAccounts/blobServices/containers:name] StringStartsWithIgnoreCase \'${aiProjectWorkspaceId}\' AND @Resource[Microsoft.Storage/storageAccounts/blobServices/containers:name] StringLikeIgnoreCase \'*-azureml-agent\'))'

resource storageBlobDataOwnerAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: storage
  name: guid(storage.id, aiProjectPrincipalId, storageBlobDataOwnerRoleDefinitionId, aiProjectWorkspaceId)
  properties: {
    roleDefinitionId: resourceId('Microsoft.Authorization/roleDefinitions', storageBlobDataOwnerRoleDefinitionId)
    principalId: aiProjectPrincipalId
    principalType: aiProjectPrincipalType
    conditionVersion: '2.0'
    condition: conditionStr
  }
}

// Cosmos DB container-level data plane assignments for the agent thread,
// system thread, and entity stores created by the capability host.

var userThreadName = '${aiProjectWorkspaceId}-thread-message-store'
var systemThreadName = '${aiProjectWorkspaceId}-system-thread-message-store'
var entityStoreName = '${aiProjectWorkspaceId}-agent-entity-store'

resource cosmosAccount 'Microsoft.DocumentDB/databaseAccounts@2024-12-01-preview' existing = {
  name: cosmosDbAccountName
}

resource database 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases@2024-12-01-preview' existing = {
  parent: cosmosAccount
  name: 'enterprise_memory'
}

resource containerUserMessageStore 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-12-01-preview' existing = {
  parent: database
  name: userThreadName
}

resource containerSystemMessageStore 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-12-01-preview' existing = {
  parent: database
  name: systemThreadName
}

resource containerEntityStore 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-12-01-preview' existing = {
  parent: database
  name: entityStoreName
}

var roleDefinitionId = resourceId(
  'Microsoft.DocumentDB/databaseAccounts/sqlRoleDefinitions',
  cosmosDbAccountName,
  '00000000-0000-0000-0000-000000000002'
)

var scopeSystemContainer = '${cosmosAccount.id}/dbs/enterprise_memory/colls/${systemThreadName}'
var scopeUserContainer = '${cosmosAccount.id}/dbs/enterprise_memory/colls/${userThreadName}'
var scopeEntityContainer = '${cosmosAccount.id}/dbs/enterprise_memory/colls/${entityStoreName}'

resource containerRoleAssignmentUserContainer 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2022-05-15' = {
  parent: cosmosAccount
  name: guid(aiProjectWorkspaceId, containerUserMessageStore.id, roleDefinitionId, aiProjectPrincipalId)
  properties: {
    principalId: aiProjectPrincipalId
    roleDefinitionId: roleDefinitionId
    scope: scopeUserContainer
  }
}

resource containerRoleAssignmentSystemContainer 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2022-05-15' = {
  parent: cosmosAccount
  name: guid(aiProjectWorkspaceId, containerSystemMessageStore.id, roleDefinitionId, aiProjectPrincipalId)
  properties: {
    principalId: aiProjectPrincipalId
    roleDefinitionId: roleDefinitionId
    scope: scopeSystemContainer
  }
}

resource containerRoleAssignmentEntityContainer 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2022-05-15' = {
  parent: cosmosAccount
  name: guid(aiProjectWorkspaceId, containerEntityStore.id, roleDefinitionId, aiProjectPrincipalId)
  properties: {
    principalId: aiProjectPrincipalId
    roleDefinitionId: roleDefinitionId
    scope: scopeEntityContainer
  }
}
