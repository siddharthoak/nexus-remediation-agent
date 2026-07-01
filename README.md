# OSS Vulnerability Remediation Agent

Automated pipeline that fetches Nexus IQ vulnerability reports for a Java/Maven
repository, upgrades vulnerable dependencies, fixes any resulting code breakage using
Claude, opens a GitHub PR, and iteratively retries CI failures — all deployed as
Hosted Agents on Azure AI Foundry.

## Architecture

```
Fixer Agent (scheduled)          Watcher Agent (poll-driven)
──────────────────────────        ──────────────────────────────
1. Fetch Nexus IQ report          1. Poll CI status on open PRs
2. Clone repo, create branch      2. CI passed → leave for review
3. Bump pom.xml version           3. CI failed → call Claude to
4. Call Claude to fix code           diagnose + apply minimal fix
5. Commit, push, open PR          4. Push fix to same branch
6. Record PR state                5. Repeat up to MAX_RETRY_ATTEMPTS
```

Both agents are containerized Python applications deployed to Azure AI Foundry as
Hosted Agents — meaning we own the full orchestration logic (loops, retries, git ops),
not AAF's conversation-loop model.

## Repo Structure

```
nexus-remediation-agent/
├── infra/
│   ├── main.bicep                   # ACR + Key Vault (INF-01)
│   └── agents/
│       ├── fixer.agent.yaml         # Fixer Hosted Agent definition (INF-02)
│       └── watcher.agent.yaml       # Watcher Hosted Agent definition (INF-02)
├── agents/
│   ├── fixer/
│   │   ├── Dockerfile
│   │   ├── instructions.md          # System prompt (reviewable as plain text)
│   │   ├── main.py                  # Entry point
│   │   ├── nexus_client.py          # FIX-01: Nexus IQ API client
│   │   ├── repo_ops.py              # FIX-02: git clone/branch/commit/push
│   │   ├── code_fixer.py            # FIX-03: Claude-driven code changes
│   │   └── pr_client.py             # FIX-04: GitHub PR creation
│   └── watcher/
│       ├── Dockerfile
│       ├── instructions.md
│       ├── main.py
│       ├── ci_status.py             # WAT-01: GitHub Checks API poller
│       └── retry_controller.py      # WAT-02: bounded retry loop
├── scripts/
│   ├── bootstrap_foundry_project.sh # SCR-01: run ONCE
│   └── update_agent.py              # SCR-02: run on every change
├── tests/
│   ├── test_nexus_client.py
│   ├── test_code_fixer.py
│   └── test_retry_controller.py
└── .github/workflows/ci.yml
```

## Setup (AAF-Access Person)

These steps require Azure AI Foundry portal / subscription-level access.

### 1. One-time infrastructure bootstrap

```bash
export AZURE_RESOURCE_GROUP=oss-remediation-rg
export AZURE_LOCATION=eastus
bash scripts/bootstrap_foundry_project.sh
```

The script will print the remaining **manual steps** you must complete in the
Azure AI Foundry portal before running the update script.

### 2. Complete manual steps (printed by bootstrap script)

- Create or confirm the Foundry project.
- Deploy a model (Claude) under "Models + Endpoints" — note the deployment name.
- Grant `Azure AI User` RBAC role to the identity running the update script.
- Grant `AcrPull` on the provisioned ACR to the Foundry project's managed identity.
- Populate Key Vault secrets:
  ```bash
  az keyvault secret set --vault-name <KV_NAME> --name github-pat --value <PAT>
  az keyvault secret set --vault-name <KV_NAME> --name nexus-iq-api-key --value <KEY>
  ```

### 3. Deploy / update agents (run on every change)

```bash
export PROJECT_ENDPOINT=https://<hub>.services.ai.azure.com/api/projects/<project>
export MODEL_DEPLOYMENT_NAME=claude-deployment
export ACR_LOGIN_SERVER=<from bootstrap output>
export KEY_VAULT_URI=<from bootstrap output>

az acr login --name <ACR_NAME>
python3 scripts/update_agent.py
```

## Running Tests

```bash
pip install -r requirements.txt
pytest tests/ -v
```

Tests run entirely offline (all external API calls are mocked).

## Open Items

- **Nexus IQ API contract**: `nexus_client.py` contains `FIXME` markers for the exact
  endpoint paths and response field names. These must be validated against the customer's
  Nexus IQ instance before the client is trusted in production. See `agents/fixer/nexus_client.py`.

- **AAF Hosted Agent YAML schema**: The `infra/agents/*.agent.yaml` files include a
  `TODO` noting the Key Vault reference syntax must be confirmed against current AAF docs.

- **AIProjectClient method signatures**: `scripts/update_agent.py` includes a `NOTE`
  about confirming `create_agent`/`update_agent` signatures against the current SDK.

## Smoke Test Checklist

After deployment, verify end-to-end flow:

- [ ] Trigger the Fixer agent against a test repo with a known vulnerable dependency.
- [ ] Confirm a PR is opened with the correct title and description.
- [ ] Break the test repo's CI deliberately; confirm the Watcher agent picks up the failure.
- [ ] Confirm the Watcher pushes a corrective commit and CI re-runs.
- [ ] Confirm the Watcher stops retrying after `MAX_RETRY_ATTEMPTS` and posts an escalation comment.
- [ ] Confirm no force-pushes and no duplicate PRs across repeated runs.
