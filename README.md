# OSS Vulnerability Remediation Agent

Automated pipeline that fetches Nexus IQ vulnerability reports for a repository
(Maven, Gradle, npm, or Python — auto-detected, including polyglot repos),
upgrades vulnerable dependencies, fixes any resulting code breakage using
Claude, opens a GitHub PR, and iteratively retries CI failures — all deployed
as Hosted Agents on Azure AI Foundry.

A Knowledge Agent and Classifier run ahead of every fix attempt: the Knowledge
Agent hydrates a persistent Knowledge Base from official release notes and
migration guides, and the Classifier uses that KB to sort each finding into
one of four buckets (no safe path / patch-minor / major-with-known-migration /
complex-framework-level) before deciding whether to invoke the Fixer at all or
open a triage GitHub Issue instead. See `PLAN.md` for the full design.

## Documentation Map

| Document | Read this for |
| :---- | :---- |
| `PLAN.md` | The full design: architecture decisions, end-to-end flow, tracking-store schema, KB tiers, classification buckets, autonomy/safety guarantees. Start here for *why*. |
| `DEPLOYMENT_AAF.md` | Step-by-step deployment to Azure AI Foundry — prerequisites, `config.yaml` setup, the two scripts, RBAC, Key Vault secrets, and a full configuration reference table (including `DEPLOYMENT_MODE`). Start here if you have AAF access and need to deploy. |
| `TESTING_DOCKER.md` / `TESTING_PODMAN.md` | Local, no-Azure-access testing of the Fixer/Watcher logic in containers — from a runtime smoke test up to a full Fixer+Watcher Cosmos-backed run. Start here if you don't have AAF access yet but want to validate the pipeline. |

## Architecture

```
                    (Discovery — deferred, see Open Items)
                                   │
                                   ▼
        Knowledge Agent — hydrates KB from release notes / migration guides
                                   │
                                   ▼
        Classifier — assigns each finding to a bucket using KB data
                                   │
                    ┌──────────────┴──────────────┐
                    ▼                              ▼
        Fixer Agent (scheduled)          GitHub Issue (no safe path /
        ──────────────────────────       complex framework-level finding)
        1. Bump dependency manifest
        2. Call Claude to fix code, verify with a framework-detected
           compile gate + unit-test gate before opening a PR
        3. Commit, push, open PR
        4. Record tracking state
                    │
                    ▼
        customer's CI pipeline runs
                    │
                    ▼
        Watcher Agent (poll-driven)
        ──────────────────────────────
        1. Poll CI status on open PRs
        2. CI passed → leave for review
        3. CI failed → invoke the Fixer again with the failure log,
           up to MAX_RETRY_ATTEMPTS, then escalate
```

The Knowledge Agent and Classifier are plain Python modules, not separate
containers — `agents/fixer/main.py` imports and runs them in-process at the
start of every fresh scan. The Fixer and Watcher are the only two things
actually deployed as **Hosted Agents**: containerized Python applications on
Azure AI Foundry where we own the full orchestration logic (loops, retries,
git ops), not AAF's conversation-loop model.

## Repo Structure

```
nexus-remediation-agent/
├── PLAN.md / DEPLOYMENT_AAF.md / TESTING_DOCKER.md / TESTING_PODMAN.md
├── streamlit_dashboard.py           # Read-only observability dashboard
├── config.yaml.example              # Copy to config.yaml — central deploy config
├── .env.example                     # Copy to .env — local Docker/Podman testing only
├── docker-compose.e2e.yml           # Local Fixer+Watcher end-to-end test (TESTING_*.md section 6)
├── infra/
│   ├── main.bicep                   # ACR + Key Vault + Cosmos DB (Serverless)
│   └── agents/
│       ├── fixer.agent.yaml         # Fixer Hosted Agent definition
│       └── watcher.agent.yaml       # Watcher Hosted Agent definition
├── agents/
│   ├── common/                      # Shared package, copied into both container images
│   │   ├── tracking_store.py        # CosmosTrackingStore / InMemoryTrackingStore
│   │   └── knowledge_store.py       # CosmosKBStore / InMemoryKBStore
│   ├── knowledge_agent/             # No Dockerfile — runs in-process inside the Fixer
│   │   ├── main.py
│   │   └── retrieval.py
│   ├── classifier/                  # No Dockerfile, no main.py — runs in-process inside the Fixer
│   │   └── rules.py
│   ├── fixer/
│   │   ├── Dockerfile               # Multi-runtime: JDK 17 + Maven, Node 20 + npm, Python 3.11;
│   │   │                              also COPYs in knowledge_agent/ and classifier/ source
│   │   ├── instructions.md          # System prompt (reviewable as plain text)
│   │   ├── main.py                  # Entry point — two trigger modes (fresh scan / Watcher retry)
│   │   ├── nexus_client.py          # NexusIQClient + make_vulnerability_source() factory
│   │   ├── scan_report_client.py    # ScanReportClient — local-mode Trivy/Grype/OWASP JSON source
│   │   ├── repo_ops.py              # git clone/branch/commit/push
│   │   ├── code_fixer.py            # Claude tool-use loop + KB-hit fast path
│   │   ├── frameworks/              # BuildFramework abstraction: maven / gradle / npm / python_pip
│   │   └── pr_client.py             # GitHub PR creation
│   └── watcher/
│       ├── Dockerfile
│       ├── instructions.md
│       ├── main.py                  # Imports RetryGate — the active retry logic
│       ├── ci_status.py             # GitHub Checks API poller
│       ├── retry_gate.py            # Bounded retry gate — zero model calls, zero file edits, zero git
│       ├── pattern_learner.py       # Writes Tier-1 KB entries after a confirmed fix
│       ├── retry_controller.py      # Legacy, unused — see Open Items
│       └── cosmos_counter.py        # Legacy, backs the unused `retry-attempts` Cosmos container
├── scripts/
│   ├── bootstrap_foundry_project.sh # Run ONCE — deploys Bicep, writes config.yaml's infra: section
│   └── update_agent.py              # Run on every change — builds/pushes images, registers agents
├── tests/
│   ├── test_nexus_client.py
│   ├── test_scan_report_client.py
│   ├── test_code_fixer.py           # Includes CodeFixer.run_retry_fix() coverage
│   ├── test_frameworks.py
│   └── test_retry_controller.py     # Tests the legacy retry_controller.py — see Open Items
└── .github/workflows/ci.yml
```

`config/` (`ignore_list.yaml` / `known_list.yaml`) and `agents/discovery/` are
part of the design (PLAN.md section 5.1) but don't exist in the repo yet —
Discovery is deferred until the customer supplies its first ignore/known
lists.

## Setup (AAF-Access Person)

These steps require Azure AI Foundry portal / subscription-level access.
**This is the condensed version — follow `DEPLOYMENT_AAF.md` for the full
walkthrough with exact commands and a complete configuration reference.**

### 1. Configuration and one-time infrastructure bootstrap

```bash
cp config.yaml.example config.yaml    # fill in values as you go (DEPLOYMENT_AAF.md sections 1–3)

export AZURE_RESOURCE_GROUP=oss-remediation-rg
export AZURE_LOCATION=eastus
bash scripts/bootstrap_foundry_project.sh
```

The script deploys `infra/main.bicep` (ACR + Key Vault + Cosmos DB), writes
its outputs directly into `config.yaml`'s `infra:` section, and prints the
remaining **manual steps** you must complete in the Azure AI Foundry portal.

### 2. Complete manual steps (printed by bootstrap script)

- Create or confirm the Foundry project; set `foundry.project_endpoint` in `config.yaml`.
- Deploy a model (Claude) under "Models + Endpoints"; set `foundry.model_deployment_name`.
- Grant `Azure AI User` RBAC role to the identity running the update script.
- Grant `AcrPull` on the provisioned ACR to the Foundry project's managed identity.
- Populate Key Vault secrets — **three**, not two:
  ```bash
  az keyvault secret set --vault-name <KV_NAME> --name github-pat --value <PAT>
  az keyvault secret set --vault-name <KV_NAME> --name nexus-iq-api-key --value <KEY>
  az keyvault secret set --vault-name <KV_NAME> --name anthropic-api-key --value <KEY>
  ```
  The Anthropic API key is easy to miss — `code_fixer.py` and the inline
  Knowledge Agent call `anthropic.Anthropic()`, which reads it directly. This
  is separate from the Foundry model deployment above; the model deployment
  name alone doesn't supply authentication.
- After the Fixer's first deploy, grant each agent's managed identity
  **Cosmos DB Built-in Data Contributor** (data-plane, not ARM Contributor)
  and set `foundry.fixer_agent_id` in `config.yaml` from the portal, then
  redeploy so the Watcher gets `FIXER_AGENT_ID`.

### 3. Deploy / update agents (run on every change)

```bash
az acr login --name <ACR_NAME>
python3 scripts/update_agent.py
```

Reads everything from `config.yaml`; no environment variables required
(though they override `config.yaml` values if set — useful for CI).

## Local Testing (No AAF Access Required)

See `TESTING_DOCKER.md` (or `TESTING_PODMAN.md`) for a from-scratch walkthrough
that needs no Anthropic key, GitHub PAT, or Azure/Cosmos access for its first
four steps: building the multi-framework Fixer image, a runtime smoke test,
exercising `detect_framework()`/`bump_dependency()`/`build()`/`test_unit()`
against fixture repos, and running the automated test suite. Later sections
cover an optional full end-to-end run against a real target repo, and a
Fixer+Watcher shared-state test against a real Cosmos DB account.

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
  Local testing bypasses this entirely via `DEPLOYMENT_MODE=local` + `ScanReportClient`.

- **AAF Hosted Agent YAML schema**: The `infra/agents/*.agent.yaml` files include a
  `TODO` noting the Key Vault `secretRef` syntax must be confirmed against current AAF docs.

- **`update_agent.py` doesn't yet wire the container spec into the Foundry SDK call**:
  `create_or_update_agent()` only passes `name`/`instructions`/`model` to
  `AIProjectClient` today — not the container image, environment variables, or
  schedule from the rendered agent YAML (see the `NOTE` comments in the script).
  Use the `*.rendered.yaml` files it writes as the source of truth for what the
  container/env/schedule *should* be until the SDK contract is confirmed and
  this is wired up. See `DEPLOYMENT_AAF.md` section 5.

- **`retry_gate.py` (`RetryGate`) has no dedicated unit test file**: `agents/watcher/main.py`
  imports `RetryGate` from `retry_gate.py` — this is the live retry-bound/escalation
  logic. `tests/test_retry_controller.py` tests a different, unused module
  (`agents/watcher/retry_controller.py`) left over from an earlier design that
  called Claude directly from the Watcher; that responsibility now lives entirely
  in the Fixer. `agents/watcher/cosmos_counter.py` is the same vintage, backing
  the now-legacy `retry-attempts` Cosmos container (superseded by
  `CosmosTrackingStore.count_attempts_for_pr()`). See `TESTING_DOCKER.md` section 6.4.

- **Discovery agent is deferred**: `agents/discovery/` and `config/ignore_list.yaml` /
  `known_list.yaml` don't exist in the repo yet. Until they do, all Nexus findings
  flow through the Knowledge Agent and Classifier unfiltered — harmless, just no
  pre-filtering of accepted-risk findings yet. See PLAN.md section 5.1 / 11.2.

## Smoke Test Checklist

After deployment, verify end-to-end flow (full version in `DEPLOYMENT_AAF.md` section 9):

- [ ] Trigger the Fixer agent against a test repo with a known vulnerable dependency.
- [ ] Confirm a PR is opened, with a passing compile/build gate in the container logs
      (`build (maven): SUCCESS`, or the equivalent for whichever framework the test
      repo uses) before the PR was created.
- [ ] Break the test repo's CI deliberately; confirm the Watcher agent picks up the failure.
- [ ] Confirm the Watcher successfully invokes the Fixer for a retry and CI re-runs.
- [ ] Confirm the Watcher stops retrying after `MAX_RETRY_ATTEMPTS` and posts an escalation comment.
- [ ] Confirm no force-pushes and no duplicate PRs across repeated runs.
