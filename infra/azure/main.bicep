// hl7bridge Azure prototype: Azure OpenAI + Azure AI Search, Entra ID only (no keys), with an
// optional private-networking phase where Azure OpenAI is reachable only through a private
// endpoint and the evaluation runs inside the VNet as a managed identity.
//
// Phase A (privateNetworking=false): run the pilot from your laptop with `az login`.
// Phase B (privateNetworking=true):  OpenAI public access off; private endpoint + private DNS;
//                                    Container Apps job in the VNet runs the eval.
// Works in commercial Azure and Azure Government (DNS zones and endpoints switch automatically).

targetScope = 'resourceGroup'

@description('Short prefix for resource names (lowercase letters/digits).')
@minLength(3)
@maxLength(10)
param prefix string = 'hl7b'

param location string = resourceGroup().location

@description('Object id of the developer (az ad signed-in-user show --query id -o tsv). Granted data-plane roles so the laptop can run Phase A. Leave empty to skip.')
param developerPrincipalId string = ''

@description('Chat model and version; confirm availability in your region (Government regions list fewer models).')
param chatModel string = 'gpt-4o-mini'
param chatModelVersion string = '2024-07-18'
param embedModel string = 'text-embedding-3-small'
param embedModelVersion string = '1'

@description('GlobalStandard is commercial-only; Azure Government uses Standard (data stays in Government regions).')
@allowed(['GlobalStandard', 'Standard', 'DataZoneStandard'])
param deploymentSku string = 'GlobalStandard'
param chatCapacity int = 10
param embedCapacity int = 10

@allowed(['basic', 'standard'])
param searchSku string = 'basic'

@description('Phase B: disable public access to Azure OpenAI and reach it through a private endpoint.')
param privateNetworking bool = false

@description('Phase B, after `az acr build`: deploy the Container Apps job that runs the eval inside the VNet.')
param deployRunner bool = false
param runnerImageTag string = 'latest'

var isGov = environment().name == 'AzureUSGovernment'
var suffix = uniqueString(resourceGroup().id)
var openAiZone = isGov ? 'privatelink.openai.azure.us' : 'privatelink.openai.azure.com'
var searchHost = isGov ? 'search.azure.us' : 'search.windows.net'
var tags = { project: 'hl7bridge', purpose: 'interview-prototype', data: 'synthetic-only' }

// Built-in role definition ids
var roles = {
  openAiUser: '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'          // Cognitive Services OpenAI User
  searchIndexContributor: '8ebe5a00-799e-43f5-93ac-243d3dce84a7' // Search Index Data Contributor
  searchServiceContributor: '7ca78c08-252a-4471-8644-bb5ff32d4ba0' // Search Service Contributor (create index)
  acrPull: '7f951dda-4ed3-4680-a7ca-43fe172d538d'
}

// ---------------------------------------------------------------- identity
resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${prefix}-runner-id'
  location: location
  tags: tags
}

// ---------------------------------------------------------------- Azure OpenAI
resource openAi 'Microsoft.CognitiveServices/accounts@2024-10-01' = {
  name: '${prefix}-aoai-${suffix}'
  location: location
  tags: tags
  kind: 'OpenAI'
  sku: { name: 'S0' }
  properties: {
    customSubDomainName: '${prefix}-aoai-${suffix}' // required for Entra ID auth
    disableLocalAuth: true                          // no API keys
    publicNetworkAccess: privateNetworking ? 'Disabled' : 'Enabled'
    networkAcls: { defaultAction: privateNetworking ? 'Deny' : 'Allow' }
  }
}

resource chat 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = {
  parent: openAi
  name: 'chat'
  sku: { name: deploymentSku, capacity: chatCapacity }
  properties: { model: { format: 'OpenAI', name: chatModel, version: chatModelVersion } }
}

resource embed 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = {
  parent: openAi
  name: 'embed'
  dependsOn: [chat] // deployments on one account must be created one at a time
  sku: { name: deploymentSku, capacity: embedCapacity }
  properties: { model: { format: 'OpenAI', name: embedModel, version: embedModelVersion } }
}

// ---------------------------------------------------------------- Azure AI Search
resource search 'Microsoft.Search/searchServices@2023-11-01' = {
  name: '${prefix}-search-${suffix}'
  location: location
  tags: tags
  sku: { name: searchSku }
  properties: {
    disableLocalAuth: true // Entra ID / RBAC only
    publicNetworkAccess: 'enabled'
    semanticSearch: 'free'
    replicaCount: 1
    partitionCount: 1
  }
}

// ---------------------------------------------------------------- role assignments
// Names must be computable before deployment, so the loop runs over static keys and derives the
// name from the identity's resource id (known up front); the principal id is resolved at deploy time.
var principalKeys = empty(developerPrincipalId) ? ['runner'] : ['runner', 'developer']

resource openAiUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for k in principalKeys: {
  scope: openAi
  name: guid(openAi.id, k == 'runner' ? identity.id : developerPrincipalId, roles.openAiUser)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.openAiUser)
    principalId: k == 'runner' ? identity.properties.principalId : developerPrincipalId
    principalType: k == 'runner' ? 'ServicePrincipal' : 'User'
  }
}]

resource searchData 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for k in principalKeys: {
  scope: search
  name: guid(search.id, k == 'runner' ? identity.id : developerPrincipalId, roles.searchIndexContributor)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.searchIndexContributor)
    principalId: k == 'runner' ? identity.properties.principalId : developerPrincipalId
    principalType: k == 'runner' ? 'ServicePrincipal' : 'User'
  }
}]

resource searchService 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for k in principalKeys: {
  scope: search
  name: guid(search.id, k == 'runner' ? identity.id : developerPrincipalId, roles.searchServiceContributor)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.searchServiceContributor)
    principalId: k == 'runner' ? identity.properties.principalId : developerPrincipalId
    principalType: k == 'runner' ? 'ServicePrincipal' : 'User'
  }
}]

// ---------------------------------------------------------------- Phase B: private networking
resource vnet 'Microsoft.Network/virtualNetworks@2023-11-01' = if (privateNetworking) {
  name: '${prefix}-vnet'
  location: location
  tags: tags
  properties: {
    addressSpace: { addressPrefixes: ['10.40.0.0/16'] }
    subnets: [
      {
        name: 'private-endpoints'
        properties: { addressPrefix: '10.40.1.0/24', privateEndpointNetworkPolicies: 'Disabled' }
      }
      {
        name: 'runner'
        properties: {
          addressPrefix: '10.40.2.0/27'
          delegations: [{ name: 'aca', properties: { serviceName: 'Microsoft.App/environments' } }]
        }
      }
    ]
  }
}

// The step that usually breaks: without this zone linked to the VNet, the OpenAI hostname still
// resolves to its public address, which now refuses connections.
resource openAiDns 'Microsoft.Network/privateDnsZones@2020-06-01' = if (privateNetworking) {
  name: openAiZone
  location: 'global'
  tags: tags
}

resource openAiDnsLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2020-06-01' = if (privateNetworking) {
  parent: openAiDns
  name: '${prefix}-vnet-link'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: { id: vnet.id }
  }
}

resource openAiEndpoint 'Microsoft.Network/privateEndpoints@2023-11-01' = if (privateNetworking) {
  name: '${prefix}-aoai-pe'
  location: location
  tags: tags
  properties: {
    subnet: { id: '${vnet.id}/subnets/private-endpoints' }
    privateLinkServiceConnections: [
      {
        name: 'aoai'
        properties: { privateLinkServiceId: openAi.id, groupIds: ['account'] }
      }
    ]
  }
}

resource openAiEndpointDns 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2023-11-01' = if (privateNetworking) {
  parent: openAiEndpoint
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [{ name: 'openai', properties: { privateDnsZoneId: openAiDns.id } }]
  }
}

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' = if (privateNetworking) {
  name: '${prefix}acr${suffix}'
  location: location
  tags: tags
  sku: { name: 'Basic' }
  properties: { adminUserEnabled: false } // pulls use the managed identity
}

resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (privateNetworking) {
  scope: registry
  name: guid(registry.id, identity.id, roles.acrPull)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.acrPull)
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = if (privateNetworking) {
  name: '${prefix}-logs'
  location: location
  tags: tags
  properties: { sku: { name: 'PerGB2018' }, retentionInDays: 30 }
}

resource appEnv 'Microsoft.App/managedEnvironments@2024-03-01' = if (privateNetworking) {
  name: '${prefix}-env'
  location: location
  tags: tags
  properties: {
    vnetConfiguration: { infrastructureSubnetId: '${vnet.id}/subnets/runner', internal: true }
    workloadProfiles: [{ name: 'Consumption', workloadProfileType: 'Consumption' }]
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logs!.properties.customerId
        sharedKey: logs!.listKeys().primarySharedKey
      }
    }
  }
}

resource evalJob 'Microsoft.App/jobs@2024-03-01' = if (privateNetworking && deployRunner) {
  name: '${prefix}-eval'
  location: location
  tags: tags
  identity: { type: 'UserAssigned', userAssignedIdentities: { '${identity.id}': {} } }
  dependsOn: [acrPull, openAiEndpointDns, openAiDnsLink]
  properties: {
    environmentId: appEnv.id
    workloadProfileName: 'Consumption'
    configuration: {
      triggerType: 'Manual'
      replicaTimeout: 1800
      replicaRetryLimit: 0
      manualTriggerConfig: { parallelism: 1, replicaCompletionCount: 1 }
      registries: [{ server: registry!.properties.loginServer, identity: identity.id }]
    }
    template: {
      containers: [
        {
          name: 'eval'
          image: '${registry!.properties.loginServer}/hl7bridge:${runnerImageTag}'
          command: ['python', '-m', 'hl7bridge', 'eval', '--provider', 'azure-openai', '--index', 'azure-search']
          resources: { cpu: json('1.0'), memory: '2Gi' }
          env: [
            { name: 'AZURE_CLIENT_ID', value: identity.properties.clientId }
            { name: 'AZURE_CLOUD', value: isGov ? 'usgovernment' : 'public' }
            { name: 'AZURE_OPENAI_ENDPOINT', value: openAi.properties.endpoint }
            { name: 'AZURE_OPENAI_DEPLOYMENT', value: chat.name }
            { name: 'AZURE_OPENAI_EMBED_DEPLOYMENT', value: embed.name }
            { name: 'AZURE_SEARCH_ENDPOINT', value: 'https://${search.name}.${searchHost}' }
            { name: 'AZURE_SEARCH_INDEX', value: 'hl7bridge-evidence' }
          ]
        }
      ]
    }
  }
}

// ---------------------------------------------------------------- outputs
output openAiEndpoint string = openAi.properties.endpoint
output chatDeployment string = chat.name
output embedDeployment string = embed.name
output searchEndpoint string = 'https://${search.name}.${searchHost}'
output identityClientId string = identity.properties.clientId
output registryName string = privateNetworking ? registry.name : ''
output evalJobName string = privateNetworking && deployRunner ? evalJob.name : ''
output cloud string = isGov ? 'usgovernment' : 'public'
