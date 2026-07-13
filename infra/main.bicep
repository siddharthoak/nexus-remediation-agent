// INF-01: Core Azure resources for OSS Vulnerability Remediation Agent
// Deploys: Azure Container Registry + Azure Key Vault (RBAC-based) + Cosmos DB
// Does NOT provision the Azure AI Foundry project — that requires portal/subscription-level access.
// Deploy via: az deployment group create --resource-group <rg> --template-file infra/main.bicep

@description('Short prefix for all resource names.')
param projectPrefix string = 'ossremediation'

@description('Azure region for all resources.')
param location string = resourceGroup().location

@allowed(['dev', 'poc', 'prod'])
@description('Deployment environment.')
param environment string = 'poc'

// Unique suffix to avoid global naming collisions for ACR and Key Vault
var suffix = uniqueString(resourceGroup().id, projectPrefix)

var acrName = '${replace(projectPrefix, '-', '')}${take(suffix, 8)}'  // ACR names: alphanumeric only
var keyVaultName = '${projectPrefix}-kv-${take(suffix, 6)}'            // KV: max 24 chars
var cosmosAccountName = '${projectPrefix}-cosmos-${take(suffix, 6)}'   // Cosmos: max 44 chars

var commonTags = {
  project: 'oss-remediation-agent'
  environment: environment
}

// ── Azure Container Registry ──────────────────────────────────────────────────
resource acr 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: acrName
  location: location
  tags: commonTags
  sku: {
    name: 'Basic'
  }
  properties: {
    adminUserEnabled: false  // Use managed identity / service principal, not admin credentials
  }
}

// ── Azure Key Vault (RBAC-based access, not legacy access policies) ───────────
resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: keyVaultName
  location: location
  tags: commonTags
  properties: {
    sku: {
      family: 'A'
      name: 'standard'
    }
    tenantId: subscription().tenantId
    enableRbacAuthorization: true       // RBAC-based access; no legacy access policies
    enableSoftDelete: true
    softDeleteRetentionInDays: 7
    enabledForDeployment: false
    enabledForDiskEncryption: false
    enabledForTemplateDeployment: false
    publicNetworkAccess: 'Enabled'      // Tighten to VNet rules in production
  }
}

// Secret placeholders — values are populated manually by the AAF-access person (never in Bicep).
// The Key Vault is provisioned empty; secrets are added via portal or az keyvault secret set.
// Expected secrets:
//   github-pat       — GitHub Personal Access Token for repo clone/push/PR operations
//   nexus-iq-api-key — Nexus IQ service account API key for vulnerability report access

// ── Cosmos DB (NoSQL) — persistent retry attempt counter for the Watcher agent ──
// Serverless capacity: no minimum cost when idle, scales up per operation on active runs.
resource cosmosAccount 'Microsoft.DocumentDB/databaseAccounts@2024-02-15-preview' = {
  name: cosmosAccountName
  location: location
  tags: commonTags
  kind: 'GlobalDocumentDB'
  properties: {
    databaseAccountOfferType: 'Standard'
    consistencyPolicy: {
      defaultConsistencyLevel: 'Session'  // Strong enough for retry counting; cheaper than Strong
    }
    locations: [
      {
        locationName: location
        failoverPriority: 0
        isZoneRedundant: false
      }
    ]
    capabilities: [
      { name: 'EnableServerless' }  // Pay-per-operation — no throughput floor for a low-volume agent
    ]
    enableAutomaticFailover: false
    enableMultipleWriteLocations: false
    // Disable public network access in production; use private endpoint instead
    publicNetworkAccess: 'Enabled'
  }
}

resource cosmosDatabase 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases@2024-02-15-preview' = {
  parent: cosmosAccount
  name: 'oss-remediation'
  properties: {
    resource: { id: 'oss-remediation' }
  }
}

// tracking-records: one document per fix attempt (Fixer creates, Watcher reads/updates).
// Used by CosmosTrackingStore. TTL 90 days — resolved records auto-expire.
resource cosmosTrackingContainer 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-02-15-preview' = {
  parent: cosmosDatabase
  name: 'tracking-records'
  properties: {
    resource: {
      id: 'tracking-records'
      partitionKey: {
        paths: ['/id']
        kind: 'Hash'
        version: 2
      }
      defaultTtl: 7776000  // 90 days in seconds
    }
  }
}

// kb-entries: knowledge base entries (tier1_learned, tier2_playbook, knowledge_agent).
// Used by CosmosKBStore. No TTL — KB entries accumulate value over time.
resource cosmosKBContainer 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-02-15-preview' = {
  parent: cosmosDatabase
  name: 'kb-entries'
  properties: {
    resource: {
      id: 'kb-entries'
      partitionKey: {
        paths: ['/id']
        kind: 'Hash'
        version: 2
      }
    }
  }
}

// retry-attempts: legacy CosmosAttemptCounter documents (kept for backwards compatibility).
// CosmosTrackingStore's count_attempts_for_pr() supersedes this in the full implementation.
resource cosmosRetryContainer 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-02-15-preview' = {
  parent: cosmosDatabase
  name: 'retry-attempts'
  properties: {
    resource: {
      id: 'retry-attempts'
      partitionKey: {
        paths: ['/id']
        kind: 'Hash'
        version: 2
      }
      defaultTtl: 7776000
    }
  }
}

// ── Log Analytics Workspace + Application Insights (OBS-02) ───────────────────
// Backs the AAF-deployment-only Azure Monitor telemetry emitted by
// agents/common/telemetry.py (DEPLOYMENT_MODE=azure only — local testing never
// touches this; see PLAN.md section 4.9). Additive to Cosmos DB above, not a
// replacement for it — Cosmos remains the Fixer/Watcher's shared source of truth
// for retry-bound enforcement; this is purely an observability/analytics layer.
//
// PerGB2018 (pay-as-you-go): first 5 GB/month ingestion is free per workspace,
// then roughly $2.30-2.76/GB depending on region; 30-day retention is set
// explicitly below (within the free retention window) to keep this at or near
// $0/month for a POC's fix-attempt volume.
resource logAnalyticsWorkspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: '${projectPrefix}-logs-${take(suffix, 6)}'
  location: location
  tags: commonTags
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
  }
}

// Workspace-based Application Insights (the only supported mode today — "classic"
// App Insights is deprecated). agents/common/telemetry.py talks to this via its
// connection string, using azure-monitor-opentelemetry's OpenTelemetry distro —
// no instrumentation key / classic SDK involved.
resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: '${projectPrefix}-appins-${take(suffix, 6)}'
  location: location
  tags: commonTags
  kind: 'other'
  properties: {
    Application_Type: 'other'
    WorkspaceResourceId: logAnalyticsWorkspace.id
    IngestionMode: 'LogAnalytics'
  }
}

// Azure Monitor Workbook — pre-built dashboard (run history, retry lineage, token
// usage by CVE / over time, resolution rate) reading the traces this project emits.
// NOTE: this Workbook JSON schema (infra/observability/remediation-workbook.json)
// has not been verified against a live Azure deployment — same caveat class as the
// Nexus IQ API contract and AAF SDK call in scripts/update_agent.py (PLAN.md
// section 6). infra/observability/queries.kql has the identical queries as a
// guaranteed-usable fallback (paste into Logs or a manually created Workbook) if
// this resource doesn't render as expected.
resource remediationWorkbook 'Microsoft.Insights/workbooks@2022-04-01' = {
  name: guid(resourceGroup().id, 'oss-remediation-workbook')
  location: location
  tags: commonTags
  kind: 'shared'
  properties: {
    displayName: 'OSS Remediation — AI Usage & Fix History'
    serializedData: replace(
      loadTextContent('observability/remediation-workbook.json'),
      '__APPINSIGHTS_RESOURCE_ID__',
      appInsights.id
    )
    version: '1.0'
    sourceId: appInsights.id
    category: 'workbook'
  }
}

// ── Outputs (consumed by scripts/bootstrap_foundry_project.sh and update_agent.py) ──
output acrLoginServer string = acr.properties.loginServer
output keyVaultUri string = keyVault.properties.vaultUri
output acrName string = acr.name
output keyVaultName string = keyVault.name
output cosmosEndpoint string = cosmosAccount.properties.documentEndpoint
output cosmosDatabaseName string = cosmosDatabase.name
output cosmosTrackingContainerName string = cosmosTrackingContainer.name
output cosmosKBContainerName string = cosmosKBContainer.name
output logAnalyticsWorkspaceName string = logAnalyticsWorkspace.name
output appInsightsName string = appInsights.name
output appInsightsConnectionString string = appInsights.properties.ConnectionString
