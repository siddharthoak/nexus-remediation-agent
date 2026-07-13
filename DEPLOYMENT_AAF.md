# Deployment Guide — Azure AI Foundry (AAF)

This walks through deploying the Fixer and Watcher as Hosted Agents on Azure AI
Foundry, using `infra/main.bicep` (Azure resources), `infra/agents/*.agent.yaml`
(Hosted Agent definitions), and `scripts/` (bootstrap + update). See PLAN.md
sections 2–5 for the architectural reasoning; this document is the practical
step list.

This is written for the **AAF-access person** (PLAN.md section 5) — the team
member with Azure AI Foundry portal / subscription-level access. Everything up
to that point (orchestration logic, Nexus parsing, retry/guardrail logic,
container builds) is already built and tested on the engineering side; nothing
in this guide requires reading or changing application code.

For local, no-Azure testing of the Fixer/Watcher logic itself, see
`TESTING_DOCKER.md` / `TESTING_PODMAN.md` instead — this guide is specifically
about getting the same containers running as AAF Hosted Agents.

---

## 0. Prerequisites

- **Azure subscription** with rights to create resource groups, or an existing
  one you have Contributor access to.
- **Azure AI Foundry access**: rights to create/confirm a Foundry project and
  deploy a model inside it (Owner/Contributor at subscription level, or
  Account Owner on an existing AI Foundry account).
- **Azure CLI** (`az`), logged in: `az login`.
- **Docker** (or Podman — swap `docker` for `podman` throughout) to build and
  push the two container images.
- **Python 3.11+** with the packages in `requirements.txt` (`PyYAML`,
  `azure-ai-projects`, `azure-identity` are the ones `scripts/` actually use):
  ```bash
  python3 -m pip install -r requirements.txt
  ```
- **A GitHub PAT** (`repo` + `pull_request` scopes) for the target repo(s) the
  Fixer will clone, branch, and open PRs against.
- **An Anthropic API key.** `code_fixer.py` and the inline Knowledge Agent both
  call `anthropic.Anthropic()` (the standard Anthropic SDK), which reads
  `ANTHROPIC_API_KEY` from the environment — this is required even though
  `MODEL_DEPLOYMENT_NAME` names a model deployed inside the Foundry project.
  The model name is passed as the `model` argument to `messages.create()`; it
  does not by itself supply authentication. Keep this distinct from the
  Foundry project's own model deployment step in Section 3.
- **Nexus IQ Server access** (base URL, API key, application public ID) if
  you're deploying against `DEPLOYMENT_MODE=azure` (the production target —
  see Section 7). If you don't have this yet, you can still complete the rest
  of this guide and swap in local scan reports later; see the note in
  Section 7's configuration table.
- This repository cloned locally.

---

## 1. Create your local `config.yaml`

All deployment configuration is centralized in one file, filled in
incrementally as you progress through this guide.

```bash
cp config.yaml.example config.yaml
```

`config.yaml` is gitignored — it holds live infra endpoints and should never
be committed. Open it now; you'll fill in most fields as you go, but a few
have workable defaults already (schedules, retry/CI-poll settings).

---

## 2. Deploy core Azure infrastructure (one-time)

```bash
az login
export AZURE_RESOURCE_GROUP=oss-remediation-rg   # or your preferred name
export AZURE_LOCATION=eastus
bash scripts/bootstrap_foundry_project.sh
```

This script (`scripts/bootstrap_foundry_project.sh`) is safe to re-run —
Bicep deployments are idempotent. It:

1. Verifies `az` is installed and you're logged in.
2. Creates the resource group if it doesn't already exist.
3. Deploys `infra/main.bicep`, which provisions:
   - An **Azure Container Registry** (Basic SKU, admin user disabled — pull
     access is granted via managed identity, not admin credentials).
   - An **Azure Key Vault** with RBAC authorization enabled (no legacy access
     policies), provisioned **empty** — secrets are populated manually in
     Section 3, never written by Bicep.
   - An **Azure Cosmos DB (Serverless)** account with database
     `oss-remediation` and three containers: `tracking-records` (90-day TTL —
     one document per fix attempt), `kb-entries` (no TTL — Knowledge Base
     entries across all three tiers), and `retry-attempts` (legacy attempt
     counter, superseded by `count_attempts_for_pr()`).
   - A **Log Analytics workspace** and **workspace-based Application
     Insights** component (OBS-02) — backs the Azure Monitor telemetry
     described in Section 3.6 and PLAN.md section 4.9. No manual step
     required for these two; see Section 3.6 for what's automatic vs. not.
   - An **Azure Monitor Workbook**, deployed from
     `infra/observability/remediation-workbook.json` — a pre-built dashboard
     reading that telemetry. Best-effort: this Workbook JSON schema has not
     been verified against a live Azure deployment (Section 12). If it
     doesn't render, `infra/observability/queries.kql` has the same queries
     to paste manually.
4. Writes the deployment outputs (ACR login server, Key Vault URI, Cosmos
   endpoint, App Insights connection string, etc.) directly into
   `config.yaml`'s `infra:` section.
5. Prints the manual steps below — keep that output visible for Section 3.

Expected output ends with `Infrastructure deployed.` followed by the four
`infra:` values and a `MANUAL STEPS REQUIRED` block.

**What this script does *not* do**: it does not create the Azure AI Foundry
project itself — that requires portal/subscription-level access this
codebase can't automate (PLAN.md section 2).

---

## 3. Manual steps in the Azure AI Foundry portal

These require the access this team doesn't have, which is why they're manual.

### 3.1 Create or confirm the Foundry project

Go to [https://ai.azure.com](https://ai.azure.com) and create a project inside
an AI hub (or confirm the one you'll use already exists). Note its **project
endpoint** — it looks like:

```
https://<hub>.services.ai.azure.com/api/projects/<project>
```

Set it in `config.yaml`:

```yaml
foundry:
  project_endpoint: "https://<hub>.services.ai.azure.com/api/projects/<project>"
```

### 3.2 Deploy a model

Under the project's **Models + Endpoints**, deploy Claude (or your chosen
model). Note the **deployment name** and set it:

```yaml
foundry:
  model_deployment_name: "claude-sonnet-5"   # whatever you named the deployment
```

### 3.3 Grant RBAC

- Grant the **Azure AI User** role, at the Foundry project scope, to whoever
  will run `scripts/update_agent.py`.
- Grant **AcrPull** on the ACR (name printed by the bootstrap script) to the
  Foundry project's managed identity — find it in the project's Identity
  blade.

(Cosmos DB data-plane access for the *agents themselves* comes later, in
Section 6, once each Hosted Agent has its own managed identity to grant it
to.)

### 3.4 Populate Key Vault secrets

Three secrets are needed — the third is easy to miss since it isn't an
Azure-native credential:

```bash
KV_NAME=<key-vault-name-from-bootstrap-output>

az keyvault secret set --vault-name "$KV_NAME" \
  --name "github-pat" --value "<your-github-pat>"

az keyvault secret set --vault-name "$KV_NAME" \
  --name "nexus-iq-api-key" --value "<your-nexus-iq-api-key>"

az keyvault secret set --vault-name "$KV_NAME" \
  --name "anthropic-api-key" --value "<your-anthropic-api-key>"
```

`infra/agents/fixer.agent.yaml` references all three by name via
`secretRef` blocks; the names must match what's under `secrets:` in
`config.yaml` (defaults shown above already match).

> **Note on the Key Vault reference syntax**: the `secretRef` blocks in
> `infra/agents/*.agent.yaml` are marked `TODO` in the source — the exact
> field names (`keyVaultUri`/`secretName`) haven't been validated against
> current AAF Hosted Agent documentation yet. Validate this against
> [the AAF Hosted Agents docs](https://learn.microsoft.com/en-us/azure/ai-foundry/agents/hosted-agents)
> before your first real deploy; the placeholder substitution mechanism
> (Section 5) is correct regardless of what the final field names turn out
> to be.

### 3.5 Fill in the remaining `config.yaml` values

```yaml
github:
  repo_target: "org/repo-name"        # the repo the Fixer will remediate

nexus:
  endpoint: "https://your-nexus-iq-server/api/v2"
  app_public_id: "your-app-public-id"
```

The `schedules:`, `runtime:`, and `secrets:` sections already have working
defaults in `config.yaml.example` — adjust only if you need different values
(e.g. a tighter `max_retry_attempts`, or a different cron cadence).

### 3.6 Observability (OBS-02) — mostly automatic

Unlike everything else in this section, **no manual portal step is required**
for the core telemetry setup — Section 2's bootstrap run already provisioned
the Log Analytics workspace, Application Insights, and the Workbook, and
wrote `infra.app_insights_connection_string` into `config.yaml`.
`scripts/update_agent.py` (Section 5) wires that value into both agents as
`APPLICATIONINSIGHTS_CONNECTION_STRING` automatically — nothing further to
configure to get the Fixer/Watcher emitting telemetry.

What you get without any extra step: an Azure Monitor Workbook ("OSS
Remediation — AI Usage & Fix History") in the resource group, with run
history, retry lineage/resolutions/escalations, token usage by CVE/component,
token usage over time, and resolved-vs-escalated trend panels. Find it under
the resource group's **Workbooks** blade, or under the Application Insights
resource's own **Workbooks** tab. If a panel renders empty, widen the time
range picker (top right) — it defaults to the last 30 days, and won't show
anything until the Fixer/Watcher have actually run at least once.

**Optional, still manual — platform-level token/request metrics at zero
code:** Azure AI Foundry model deployments emit their own token/request/
latency metrics to Azure Monitor if diagnostic settings are enabled on the
Foundry project resource. This can't be automated by our Bicep (the Foundry
project isn't a resource we provision — Section 3.1). To enable it: in the
Foundry project's **Diagnostic settings** blade, add a setting sending logs
and metrics to the Log Analytics workspace named in your bootstrap output
(`infra.log_analytics_workspace_name` in `config.yaml`). This gives a second,
code-free cross-check against the custom `FixAttemptCompleted` token totals —
useful for confirming the two roughly agree, though only the custom events
can attribute tokens back to a specific CVE/component.

**If telemetry isn't provisioned or is misconfigured, nothing breaks.**
`agents/common/telemetry.py` is designed so a missing or invalid
`APPLICATIONINSIGHTS_CONNECTION_STRING` degrades to "telemetry disabled,
logged once" — the Fixer and Watcher run exactly as they would with no
telemetry configured at all. Check the container logs for
`Azure Monitor telemetry failed to initialize` (an `ERROR`-level line) if you
expect telemetry to be active and the Workbook stays empty.

---

## 4. Authenticate Docker to your ACR

```bash
ACR_NAME=<acr-name-from-bootstrap-output>
az acr login --name "$ACR_NAME"
```

---

## 5. Build, push, and register the Hosted Agents

```bash
python3 scripts/update_agent.py
```

Run this **every time you ship a change** — it's idempotent (creates agents
if they don't exist, updates them if they do). What it does, in order:

1. Reads `config.yaml` and resolves every `{{PLACEHOLDER}}` used across both
   agent YAML files (environment variables take precedence over `config.yaml`
   if both are set — useful for CI).
2. Renders `infra/agents/fixer.agent.yaml` and `watcher.agent.yaml` with those
   values substituted, writing `*.rendered.yaml` previews next to the
   originals (these are for your review — see the caveat below).
3. Builds `fixer-agent:<git-sha>` and `watcher-agent:<git-sha>` from
   `agents/fixer/Dockerfile` / `agents/watcher/Dockerfile`, tagged with the
   current short git SHA, and pushes both to your ACR.
4. Connects to your Foundry project (`AIProjectClient` +
   `DefaultAzureCredential`) and creates-or-updates each agent by name.

Expected output ends with `All agents deployed successfully.` and the image
tag used.

> **Known gap — read before assuming this fully wires up the container:**
> `create_or_update_agent()` in `scripts/update_agent.py` today only passes
> `name`, `instructions`, and `model` to the Foundry SDK call — it does
> **not** yet pass the container image, environment variables, schedule, or
> resource limits from the rendered agent YAML (see the `NOTE` comments in
> the script and the `container_image` line commented out in
> `agent_kwargs`). This is a placeholder pending confirmation of the current
> `azure-ai-projects` SDK's actual parameter names for attaching a container
> spec to a Hosted Agent. **Until that's resolved**, use the
> `*.rendered.yaml` files this step writes as the source of truth for what
> the container/env/schedule *should* be, and complete the container wiring
> for each agent manually in the AAF portal (or extend
> `create_or_update_agent()` once the SDK contract is confirmed). This is the
> same class of open item as the Key Vault `secretRef` syntax in Section 3.4
> — both are called out explicitly in the source rather than silently
> guessed at.

---

## 6. Grant Cosmos DB data-plane access to each agent

Only possible after Section 5 creates the agents, since each Hosted Agent
gets its own system-assigned managed identity at creation time.

```bash
RESOURCE_GROUP=oss-remediation-rg   # same value used in Section 2
COSMOS_ACCOUNT=$(az cosmosdb list -g "$RESOURCE_GROUP" --query '[0].name' -o tsv)
COSMOS_SCOPE=$(az cosmosdb show -g "$RESOURCE_GROUP" -n "$COSMOS_ACCOUNT" --query id -o tsv)

# Repeat once per agent (Fixer, Watcher) — get each principal ID from the
# agent's Identity blade in the AAF portal.
az cosmosdb sql role assignment create \
  --account-name "$COSMOS_ACCOUNT" \
  --resource-group "$RESOURCE_GROUP" \
  --role-definition-name "Cosmos DB Built-in Data Contributor" \
  --principal-id "<agent-managed-identity-principal-id>" \
  --scope "$COSMOS_SCOPE"
```

This is the **data-plane** role, not ARM Contributor — without it, both
`CosmosTrackingStore` and `CosmosKBStore` will authenticate successfully
(via `DefaultAzureCredential`, which a managed identity satisfies
automatically) but every read/write will fail with an authorization error.

---

## 7. Wire the Watcher up to the Fixer, then redeploy

The Watcher needs the Fixer's AAF agent ID to invoke it for retries
(`AafFixerInvoker` in `agents/watcher/retry_gate.py`). This value doesn't
exist until after the Fixer's first deploy, so it's a two-pass setup:

1. After Section 5's first run, find the Fixer agent's ID in the AAF portal.
2. Set it in `config.yaml`:
   ```yaml
   foundry:
     fixer_agent_id: "<fixer-agent-id-from-portal>"
   ```
3. Re-run `python3 scripts/update_agent.py` so the Watcher picks up
   `FIXER_AGENT_ID` in its environment.

`update_agent.py` prints a reminder of this at the end of every run.

---

## 8. Configuration Reference

Every value below is either set in `config.yaml` (substituted into the
agent YAML templates at deploy time) or hardcoded directly in
`infra/agents/*.agent.yaml`. None of it is read from `.env` in production —
`.env` is the *local Docker/Podman testing* mechanism (see
`TESTING_DOCKER.md`), a separate path from this deployment flow.

### `DEPLOYMENT_MODE` — the most important one

`DEPLOYMENT_MODE=azure` is set **directly in both `infra/agents/fixer.agent.yaml`
and `watcher.agent.yaml`** (not templated from `config.yaml` — it's a fixed
constant for this deployment target, since a Hosted Agent always needs the
persistent/production backends). It controls three independent factory
functions, all following the same pattern (PLAN.md section 4.4a):

| Component | `DEPLOYMENT_MODE=azure` | `DEPLOYMENT_MODE=local` (not used here) |
| :---- | :---- | :---- |
| `make_tracking_store()` | `CosmosTrackingStore` | `InMemoryTrackingStore` |
| `make_knowledge_store()` | `CosmosKBStore` | `InMemoryKBStore` |
| `make_vulnerability_source()` | `NexusIQClient` (real Nexus IQ Server) | `ScanReportClient` (local Trivy/Grype/OWASP JSON) |

Before this was added explicitly, all three factories would still resolve to
their `azure` backends *implicitly* as long as `COSMOS_ENDPOINT` /
`NEXUS_IQ_ENDPOINT` were set (the "unset" fallback in each factory) — setting
`DEPLOYMENT_MODE=azure` explicitly removes that ambiguity and makes the
production configuration self-documenting rather than relying on convention.

If you need to point a deployed Fixer at local scan reports instead of a live
Nexus IQ Server (e.g. Nexus IQ access isn't ready yet — PLAN.md section 6),
override `DEPLOYMENT_MODE` to `local` for that agent and mount/provide
`SCAN_REPORT_PATH`; this isn't the intended steady state for a Hosted Agent
but unblocks testing the rest of the pipeline against a real target repo.

### Full variable reference

| `config.yaml` key | Injected as | Used by | Required | Notes |
| :---- | :---- | :---- | :---- | :---- |
| `foundry.project_endpoint` | — (used by `update_agent.py` itself, and by `PROJECT_ENDPOINT` env var) | update script, Watcher | Yes | From Section 3.1 |
| `foundry.model_deployment_name` | `MODEL_DEPLOYMENT_NAME` | Fixer, update script | Yes | From Section 3.2; passed to `client.messages.create(model=...)` |
| `foundry.fixer_agent_id` | `FIXER_AGENT_ID` | Watcher | Yes (2nd deploy) | From Section 7 — Watcher fails at startup without *some* value, even a placeholder |
| `github.repo_target` | `GITHUB_REPO_TARGET` | Fixer, Watcher | Yes | `"org/repo"` format |
| `nexus.endpoint` | `NEXUS_IQ_ENDPOINT` | Fixer | Yes (for `azure` mode) | Nexus IQ Server base URL |
| `nexus.app_public_id` | `NEXUS_IQ_APP_PUBLIC_ID` | Fixer | Yes (for `azure` mode) | Nexus IQ application public ID |
| `schedules.fixer` | agent trigger schedule | Fixer | Yes | Cron syntax, UTC; default weekly |
| `schedules.watcher` | agent trigger schedule | Watcher | Yes | Cron syntax, UTC; default every 15 min |
| `runtime.max_retry_attempts` | `MAX_RETRY_ATTEMPTS` | Fixer, Watcher | Yes | Hard upper bound on CI-fix retries per PR — enforced independently by both (PLAN.md section 4.5) |
| `runtime.ci_poll_interval` | `CI_POLL_INTERVAL` | Watcher | Yes | Seconds between CI status checks |
| `runtime.ci_timeout_seconds` | `CI_TIMEOUT_SECONDS` | Watcher | Yes | Max wait before giving up on one poll cycle |
| `runtime.cosmos_database` | `COSMOS_DATABASE` | Fixer, Watcher | Yes | Must match the Bicep-provisioned database (`oss-remediation` by default) |
| `runtime.cosmos_container` | `COSMOS_CONTAINER` | Fixer, Watcher | Yes | Tracking-records container name |
| — (no config.yaml key yet; container default is used) | `COSMOS_KB_CONTAINER` | Fixer, Watcher | No | Defaults to `kb-entries` in `knowledge_store.py`, matching the Bicep-provisioned container — only add this to the agent YAML if you rename the container |
| `secrets.nexus_iq_api_key` | `NEXUS_IQ_API_KEY` (via Key Vault `secretRef`) | Fixer | Yes (for `azure` mode) | Secret **name** in Key Vault, not the value |
| `secrets.github_pat` | `GITHUB_PAT` (via Key Vault `secretRef`) | Fixer, Watcher\* | Yes | \*Watcher only needs it if it posts PR comments directly rather than through the Fixer |
| `secrets.anthropic_api_key` | `ANTHROPIC_API_KEY` (via Key Vault `secretRef`) | Fixer | Yes | Added in this pass — see Section 0; without it, `anthropic.Anthropic()` fails at first Claude call |
| `infra.acr_login_server` | — (image reference prefix) | update script | Yes | Auto-filled by bootstrap script |
| `infra.key_vault_uri` | — (`keyVaultUri` in each `secretRef`) | update script | Yes | Auto-filled by bootstrap script |
| `infra.cosmos_endpoint` | `COSMOS_ENDPOINT` | Fixer, Watcher | Yes | Auto-filled by bootstrap script |
| `infra.app_insights_connection_string` | `APPLICATIONINSIGHTS_CONNECTION_STRING` | Fixer, Watcher | No (OBS-02) | Auto-filled by bootstrap script (Section 3.6); plain value, not a `secretRef` (same convention as `cosmos_endpoint`) — a missing/blank value disables telemetry safely, it never blocks a fix run |
| `infra.log_analytics_workspace_name` | — (used to find the Workbook / configure Foundry diagnostic settings) | you, manually | No | Auto-filled by bootstrap script; see Section 3.6's optional platform-metrics step |
| `infra.app_insights_name` | — (informational) | you, manually | No | Auto-filled by bootstrap script |
| — (hardcoded in agent YAML, not `config.yaml`) | `DEPLOYMENT_MODE=azure` | Fixer, Watcher | Yes | See above — added in this pass |

---

## 9. Smoke test

Once Sections 2–7 are complete, verify the whole pipeline end-to-end against
a **disposable test repo** (never a production repo for this first run):

- [ ] Trigger the Fixer agent once (either wait for its schedule or invoke it
      manually from the AAF portal) against a test repo with a known
      vulnerable dependency.
- [ ] Confirm a PR is opened, and that the container logs show a passing
      compile gate — e.g. `build (maven): SUCCESS` (or the equivalent for
      whichever framework the test repo uses — PLAN.md section 4.7) — before
      the PR was created.
- [ ] Confirm the tracking record exists (via the Streamlit dashboard,
      Section 10, or directly in Cosmos) with `status=PR_OPENED` or
      `CI_PENDING`.
- [ ] Deliberately break the test repo's CI on that PR.
- [ ] Confirm the Watcher agent detects the failure on its next poll cycle,
      creates a `RETRY_REQUESTED` tracking record, and successfully invokes
      the Fixer (this hand-off is the one part that **cannot** be exercised
      in local Docker/Podman testing — see `TESTING_DOCKER.md` section 6.3 —
      so this is the first real end-to-end proof it works).
- [ ] Confirm the Watcher stops retrying after `MAX_RETRY_ATTEMPTS` and posts
      an escalation comment, writing `FAILED_MAX_RETRIES`.
- [ ] Confirm no force-pushes and no duplicate PRs across repeated Fixer
      runs against the same open PR.
- [ ] (OBS-02) Confirm telemetry is flowing: open the Azure Monitor Workbook
      (Section 3.6) and check the "fix attempts (run history)" panel shows
      the run you just triggered, with non-zero token counts. If it's empty,
      check the Fixer/Watcher container logs for
      `Azure Monitor telemetry initialized` (confirms it's active) vs.
      `Azure Monitor telemetry failed to initialize` (confirms it's disabled
      and why — the fix itself should still have succeeded either way).

---

## 10. Optional: Streamlit against the deployed Cosmos account

The Azure Monitor Workbook (Section 3.6) is the AAF deployment's actual
observability surface — this section is a secondary, ad hoc option for
pointing the local Streamlit dashboard (OBS-01, PLAN.md section 7) at the
same live Cosmos data, useful for quick debugging without opening the Azure
portal.

```bash
python3 -m pip install streamlit pandas
export DEPLOYMENT_MODE=azure
export COSMOS_ENDPOINT=<same endpoint as in config.yaml>
az login
streamlit run streamlit_dashboard.py
```

Your own `az login` identity needs the narrower **Cosmos DB Built-in Data
Reader** role on the same Cosmos account (the dashboard never writes) — grant
it the same way as Section 6, with `--role-definition-name "Cosmos DB Built-in Data Reader"`.

---

## 11. Steady state — shipping subsequent changes

Once the above is done once, ongoing changes only need:

```bash
az acr login --name "$ACR_NAME"
python3 scripts/update_agent.py
```

No need to re-run `bootstrap_foundry_project.sh` (infra doesn't change) or
repeat the manual portal steps, unless you're rotating secrets or changing
RBAC.

---

## 12. Known Gaps to Track

These are called out inline above but worth collecting in one place — none
of them block completing this guide, but all affect production-readiness:

- **Nexus IQ API contract unconfirmed** (PLAN.md section 6): every endpoint
  path and response field in `nexus_client.py` is marked `# FIXME`. Local
  testing can bypass this entirely via `DEPLOYMENT_MODE=local` +
  `ScanReportClient`, but a real `azure`-mode deploy needs this resolved.
- **`update_agent.py`'s Foundry SDK call doesn't yet attach the container
  spec** (Section 5) — confirm the current `azure-ai-projects` SDK's
  parameter names for container image / environment / schedule before
  relying on `update_agent.py` alone to fully deploy a working Hosted Agent.
- **Key Vault `secretRef` syntax in `infra/agents/*.agent.yaml` is
  unconfirmed** (Section 3.4) against current AAF Hosted Agent docs.
- **Discovery agent is deferred** (PLAN.md section 5.1 / 11.2) — until
  `config/ignore_list.yaml` / `known_list.yaml` exist, all Nexus findings
  flow through the Knowledge Agent and Classifier unfiltered; this doesn't
  block deployment, it just means no pre-filtering of accepted-risk findings
  yet.
- **The Azure Monitor Workbook JSON (`infra/observability/remediation-workbook.json`)
  is unverified against a live Azure deployment** (Section 3.6, PLAN.md
  section 4.9) — same class of gap as the two items above it. The KQL itself
  (`infra/observability/queries.kql`) is lower-risk since it's plain text to
  paste in, not a schema that has to deploy correctly as an ARM resource.
- **The KB-hit fast path described for `code_fixer.py` earlier in this
  repo's design docs is not actually implemented** (PLAN.md section 4.9) —
  every fix, including Bucket 3 (major-with-known-migration), runs the full
  Claude tool-use loop today; no KB context is injected into the prompt.
  Discovered and partially fixed (a crash-causing stray `kb_entry` keyword
  argument) while wiring up OBS-02 telemetry, since that crash would have
  silently prevented the new telemetry from ever firing on fresh scans.
