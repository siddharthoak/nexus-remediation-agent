#!/usr/bin/env bash
# SCR-01: One-time bootstrap script for the OSS Remediation Agent infrastructure.
#
# Run ONCE by the team member who has Azure CLI access.
# Safe to re-run (idempotent) — Bicep deployments are idempotent by design.
#
# Prerequisites:
#   cp config.yaml.example config.yaml   ← do this first, fill in your values
#   az login
#
# Usage:
#   bash scripts/bootstrap_foundry_project.sh
#
# Azure resource group and location are read from config.yaml if set,
# or from environment variables AZURE_RESOURCE_GROUP / AZURE_LOCATION.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
BICEP_FILE="${REPO_ROOT}/infra/main.bicep"
CONFIG_FILE="${REPO_ROOT}/config.yaml"

# ── Verify config.yaml exists ─────────────────────────────────────────────────
if [[ ! -f "${CONFIG_FILE}" ]]; then
  echo "ERROR: config.yaml not found."
  echo "  Run: cp config.yaml.example config.yaml"
  echo "  Then fill in your values before running this script."
  exit 1
fi

# ── Read config values (env vars take precedence) ─────────────────────────────
# Use Python to read config.yaml so we don't need yq installed.
_cfg() {
  python3 -c "
import yaml, sys
with open('${CONFIG_FILE}') as f:
    c = yaml.safe_load(f)
keys = '$1'.split('.')
val = c
for k in keys:
    val = val.get(k, '') if isinstance(val, dict) else ''
print(val or '', end='')
"
}

RESOURCE_GROUP="${AZURE_RESOURCE_GROUP:-$(_cfg infra.azure_resource_group)}"
RESOURCE_GROUP="${RESOURCE_GROUP:-oss-remediation-rg}"

LOCATION="${AZURE_LOCATION:-eastus}"
ENVIRONMENT="${AZURE_ENVIRONMENT:-poc}"
PROJECT_PREFIX="${PROJECT_PREFIX:-ossremediation}"
DEPLOYMENT_NAME="oss-remediation-bootstrap-$(date +%Y%m%d%H%M%S)"

echo "================================================================"
echo "  OSS Remediation Agent — Bootstrap Script"
echo "================================================================"
echo ""
echo "Resource group : ${RESOURCE_GROUP}"
echo "Location       : ${LOCATION}"
echo "Environment    : ${ENVIRONMENT}"
echo "Project prefix : ${PROJECT_PREFIX}"
echo ""

# ── Step 1: Verify az CLI ─────────────────────────────────────────────────────
echo ">>> Checking Azure CLI..."
if ! command -v az &>/dev/null; then
  echo "ERROR: Azure CLI ('az') is not installed."
  echo "Install from: https://learn.microsoft.com/en-us/cli/azure/install-azure-cli"
  exit 1
fi

if ! az account show &>/dev/null; then
  echo "ERROR: Not logged in to Azure. Run 'az login' first."
  exit 1
fi

SUBSCRIPTION_ID=$(az account show --query id -o tsv)
echo "  Logged in. Subscription: ${SUBSCRIPTION_ID}"
echo ""

# ── Step 2: Ensure resource group exists ──────────────────────────────────────
echo ">>> Creating resource group '${RESOURCE_GROUP}' (if it doesn't exist)..."
az group create \
  --name "${RESOURCE_GROUP}" \
  --location "${LOCATION}" \
  --tags project=oss-remediation-agent environment="${ENVIRONMENT}" \
  --output none
echo "  Resource group ready."
echo ""

# ── Step 3: Deploy Bicep ──────────────────────────────────────────────────────
echo ">>> Deploying infra/main.bicep to resource group '${RESOURCE_GROUP}'..."
DEPLOYMENT_OUTPUT=$(az deployment group create \
  --resource-group "${RESOURCE_GROUP}" \
  --template-file "${BICEP_FILE}" \
  --name "${DEPLOYMENT_NAME}" \
  --parameters \
    projectPrefix="${PROJECT_PREFIX}" \
    location="${LOCATION}" \
    environment="${ENVIRONMENT}" \
  --query "properties.outputs" \
  --output json)

echo "  Bicep deployment complete."
echo ""

# ── Step 4: Extract outputs ───────────────────────────────────────────────────
ACR_LOGIN_SERVER=$(echo "${DEPLOYMENT_OUTPUT}" | python3 -c "import sys,json; print(json.load(sys.stdin)['acrLoginServer']['value'])")
KEY_VAULT_URI=$(echo "${DEPLOYMENT_OUTPUT}"    | python3 -c "import sys,json; print(json.load(sys.stdin)['keyVaultUri']['value'])")
ACR_NAME=$(echo "${DEPLOYMENT_OUTPUT}"         | python3 -c "import sys,json; print(json.load(sys.stdin)['acrName']['value'])")
KEY_VAULT_NAME=$(echo "${DEPLOYMENT_OUTPUT}"   | python3 -c "import sys,json; print(json.load(sys.stdin)['keyVaultName']['value'])")
COSMOS_ENDPOINT=$(echo "${DEPLOYMENT_OUTPUT}"  | python3 -c "import sys,json; print(json.load(sys.stdin)['cosmosEndpoint']['value'])")
LOG_ANALYTICS_WORKSPACE_NAME=$(echo "${DEPLOYMENT_OUTPUT}" | python3 -c "import sys,json; print(json.load(sys.stdin)['logAnalyticsWorkspaceName']['value'])")
APP_INSIGHTS_NAME=$(echo "${DEPLOYMENT_OUTPUT}" | python3 -c "import sys,json; print(json.load(sys.stdin)['appInsightsName']['value'])")
APP_INSIGHTS_CONNECTION_STRING=$(echo "${DEPLOYMENT_OUTPUT}" | python3 -c "import sys,json; print(json.load(sys.stdin)['appInsightsConnectionString']['value'])")

echo "================================================================"
echo "  Infrastructure deployed."
echo "================================================================"
echo ""
echo "  ACR_LOGIN_SERVER              = ${ACR_LOGIN_SERVER}"
echo "  KEY_VAULT_URI                 = ${KEY_VAULT_URI}"
echo "  ACR_NAME                      = ${ACR_NAME}"
echo "  KEY_VAULT_NAME                = ${KEY_VAULT_NAME}"
echo "  COSMOS_ENDPOINT               = ${COSMOS_ENDPOINT}"
echo "  LOG_ANALYTICS_WORKSPACE_NAME  = ${LOG_ANALYTICS_WORKSPACE_NAME}"
echo "  APP_INSIGHTS_NAME             = ${APP_INSIGHTS_NAME}"
echo "  APP_INSIGHTS_CONNECTION_STRING = (written to config.yaml — not printed; see note in step 3 below)"
echo ""

# ── Step 5: Write infra outputs into config.yaml ─────────────────────────────
echo ">>> Writing infra outputs to config.yaml..."
python3 - <<PYEOF
import yaml, re

with open('${CONFIG_FILE}', 'r') as f:
    content = f.read()

# Targeted line-by-line replacement to preserve comments and key order.
replacements = {
    'acr_login_server':              '${ACR_LOGIN_SERVER}',
    'acr_name':                      '${ACR_NAME}',
    'key_vault_uri':                 '${KEY_VAULT_URI}',
    'key_vault_name':                '${KEY_VAULT_NAME}',
    'cosmos_endpoint':                '${COSMOS_ENDPOINT}',
    'azure_resource_group':          '${RESOURCE_GROUP}',
    'log_analytics_workspace_name':  '${LOG_ANALYTICS_WORKSPACE_NAME}',
    'app_insights_name':             '${APP_INSIGHTS_NAME}',
    'app_insights_connection_string': '${APP_INSIGHTS_CONNECTION_STRING}',
}

lines = content.splitlines()
output = []
for line in lines:
    replaced = False
    for key, value in replacements.items():
        # Match lines like "  key_name: ..." under the infra: section
        if re.match(rf'^\s+{re.escape(key)}\s*:', line):
            indent = len(line) - len(line.lstrip())
            output.append(' ' * indent + f'{key}: "{value}"')
            replaced = True
            break
    if not replaced:
        output.append(line)

with open('${CONFIG_FILE}', 'w') as f:
    f.write('\n'.join(output) + '\n')

print('  config.yaml updated with infra outputs.')
PYEOF
echo ""

# ── Step 6: Print manual steps ───────────────────────────────────────────────
echo "================================================================"
echo "  MANUAL STEPS REQUIRED"
echo "================================================================"
echo ""
echo "  1. CREATE THE FOUNDRY PROJECT (if it doesn't exist):"
echo "     - Go to https://ai.azure.com and create a project in your AI hub."
echo "     - Note the project endpoint URL."
echo "     - Set it in config.yaml:  foundry.project_endpoint"
echo ""
echo "  2. DEPLOY A MODEL inside the Foundry project:"
echo "     - Under 'Models + Endpoints', deploy Claude (or another model)."
echo "     - Set the deployment name in config.yaml:  foundry.model_deployment_name"
echo ""
echo "  3. GRANT RBAC:"
echo "     - Grant 'Azure AI User' role to whoever runs update_agent.py"
echo "       at the Foundry project scope."
echo "     - Grant 'AcrPull' role on ACR '${ACR_NAME}' to the Foundry project's"
echo "       managed identity (find it in the project's Identity blade)."
echo ""
echo "  4. POPULATE KEY VAULT SECRETS:"
echo "     az keyvault secret set --vault-name '${KEY_VAULT_NAME}' \\"
echo "       --name 'nexus-iq-api-key' --value '<your-nexus-iq-api-key>'"
echo "     az keyvault secret set --vault-name '${KEY_VAULT_NAME}' \\"
echo "       --name 'github-pat' --value '<your-github-pat>'"
echo "     az keyvault secret set --vault-name '${KEY_VAULT_NAME}' \\"
echo "       --name 'anthropic-api-key' --value '<your-anthropic-api-key>'"
echo "       (code_fixer.py calls anthropic.Anthropic(), which reads this env var"
echo "        directly — required even though the model is deployed inside Foundry)"
echo ""
echo "  4b. GRANT COSMOS DB DATA ACCESS to each agent managed identity:"
echo "      (Get principal IDs from the AAF portal after deploying the agents)"
echo "      COSMOS_ACCOUNT=\$(az cosmosdb list -g '${RESOURCE_GROUP}' --query '[0].name' -o tsv)"
echo "      az cosmosdb sql role assignment create \\"
echo "        --account-name \"\${COSMOS_ACCOUNT}\" \\"
echo "        --resource-group '${RESOURCE_GROUP}' \\"
echo "        --role-definition-name 'Cosmos DB Built-in Data Contributor' \\"
echo "        --principal-id '<agent-managed-identity-principal-id>' \\"
echo "        --scope \"\$(az cosmosdb show -g '${RESOURCE_GROUP}' -n \"\${COSMOS_ACCOUNT}\" --query id -o tsv)\""
echo "      NOTE: This is the DATA PLANE role (not ARM Contributor)."
echo ""
echo "  5. THEN RUN:"
echo "     python3 scripts/update_agent.py"
echo ""
echo "  NOTE — OBSERVABILITY (OBS-02): Log Analytics workspace '${LOG_ANALYTICS_WORKSPACE_NAME}'"
echo "  and Application Insights '${APP_INSIGHTS_NAME}' were just deployed automatically —"
echo "  no manual portal step needed for these two. config.yaml's"
echo "  infra.app_insights_connection_string was populated above and update_agent.py"
echo "  wires it into both agents as APPLICATIONINSIGHTS_CONNECTION_STRING. A best-effort"
echo "  Azure Monitor Workbook was also deployed to this resource group — open it from"
echo "  the resource group's Workbooks blade, or Application Insights '${APP_INSIGHTS_NAME}' >"
echo "  Workbooks. If it doesn't render correctly, infra/observability/queries.kql has"
echo "  the same queries to paste into a manually created Workbook or the Logs blade —"
echo "  see DEPLOYMENT_AAF.md section 3.6 for the full writeup, including the free"
echo "  optional step of enabling diagnostic settings on the Foundry model deployment"
echo "  itself for platform-level token/request metrics at zero extra code."
echo ""
echo "================================================================"
