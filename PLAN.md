# OSS Vulnerability Remediation Agent — Build & Deployment Plan

---

## 1\. Problem Statement

Premier's repositories accumulate Open Source Software (OSS) vulnerability backlogs faster than developers can address them. We want an automated pipeline that:

1. Fetches vulnerability scan results for a Java repository from Nexus.  
2. Uses an AI agent to fix the vulnerable dependencies and any resulting code breakage (deprecated APIs, signature changes, etc.).  
3. Opens a Pull Request with the fix, which triggers the customer's existing CI pipeline (unit \+ integration tests).  
4. Uses a second agent to watch that PR. If CI fails, it inspects the logs and invokes the Fixer agent with the failure context — repeating up to a bounded limit — so the change is iteratively verified before it's left for human review.

**Constraint:** This must be deployed to **Azure AI Foundry (AAF)**. We do not have AAF access. We need to build everything as code in a GitHub repo such that the AAF-access person can run a small number of scripts to (a) set things up the first time and (b) push updates afterward without redoing setup.

---

## 2\. Two Kinds of Azure AI Foundry Agents (and which one we need)

AAF currently supports two agent models. This distinction matters for the design:

|  | Prompt Agent | Hosted Agent |
| :---- | :---- | :---- |
| What it is | Instructions \+ tool definitions registered directly against a Foundry project via SDK/REST | Your own containerized application code, deployed to run inside Foundry with a managed endpoint |
| Control flow | Foundry manages the conversation loop; tools are called turn-by-turn | You write the full orchestration logic yourself — loops, retries, polling, branching |
| Fits our case? | No — too limited for multi-step git operations, polling CI status, bounded retry loops | **Yes — this is what we need** |

**Decision: both the Fixer and the Watcher are Hosted Agents** — i.e., our own Python code in containers, deployed to Foundry, registered with a model deployment for the LLM calls. Foundry gives each Hosted Agent a managed identity automatically; it does not handle our control-flow logic for us.

---

## 3\. Repo Structure (with rationale per section)

```
nexus-remediation-agent/
├── README.md
├── PLAN.md
├── requirements.txt
├── streamlit_dashboard.py              ← OBS-01: observability dashboard (co-branded Neurealm × Premier)
├── infra/
│   ├── main.bicep                      ← ACR, Key Vault, Cosmos DB (Serverless)
│   └── agents/
│       ├── fixer.agent.yaml
│       └── watcher.agent.yaml
├── config/                             ← [DEFERRED — needed by Discovery agent, not yet built]
│   ├── ignore_list.yaml                ← CVEs / libraries the team has decided to skip (accepted risk, not exploitable)
│   └── known_list.yaml                 ← upgrades known to be unsafe or blocked (forks, legal holds, EOL no successor)
├── agents/
│   ├── common/                         ← shared package, all containers copy this
│   │   ├── __init__.py
│   │   ├── tracking_store.py           ← INF-03: persistent tracking record store (Cosmos + InMemory)
│   │   └── knowledge_store.py          ← KB read/write abstraction (CosmosKBStore + InMemoryKBStore)
│   ├── discovery/                      ← [DEFERRED — post-POC; see Section 3 note below]
│   │   ├── Dockerfile                  ← lightweight; no Maven, no git
│   │   ├── main.py                     ← reads Nexus report; applies ignore/known lists; creates Issues for filtered findings; outputs actionable list
│   │   └── list_filter.py              ← loads ignore_list.yaml + known_list.yaml; matches against findings; no model calls
│   ├── knowledge_agent/
│   │   ├── Dockerfile                  ← lightweight; web search tools only
│   │   ├── main.py                     ← per-(component, old_version, new_version): web search → parse → KB write
│   │   └── retrieval.py                ← web search scoped to trusted domains; structured extraction
│   ├── classifier/                     ← runs after KB hydration; classification is now KB-informed
│   │   ├── Dockerfile                  ← lightweight; no Maven, no git
│   │   ├── main.py                     ← reads KB entries per finding; assigns bucket; creates Issues for non-fixable; invokes Fixer for fixable
│   │   └── rules.py                    ← bucket logic informed by KB data; buckets 3/4 require KB entry to distinguish
│   ├── fixer/
│   │   ├── Dockerfile                  ← multi-runtime base: JDK 17 + Maven, Node 20 + npm, Python 3.11
│   │   ├── instructions.md
│   │   ├── main.py                     ← two trigger modes; parallel fresh scan + Watcher retry
│   │   ├── nexus_client.py
│   │   ├── repo_ops.py
│   │   ├── code_fixer.py               ← KB-hit path (direct tool apply, no LLM) + tool-use loop fallback
│   │   │                                  (grep_files, read_file, apply_file_change,
│   │   │                                   run_build, run_unit_tests) + InvalidRetryError guard
│   │   ├── frameworks/
│   │   │   ├── __init__.py             ← BuildFramework ABC + detect_framework() factory
│   │   │   ├── maven.py                ← pom.xml XML parse; mvn compile -q; mvn test (surefire only)
│   │   │   ├── gradle.py               ← build.gradle parse; ./gradlew build; ./gradlew test
│   │   │   ├── npm.py                  ← package.json JSON parse; npm ci + npm run build; npm test
│   │   │   └── python_pip.py           ← requirements.txt / pyproject.toml; py_compile / ruff; pytest -m "not integration"
│   │   └── pr_client.py
│   └── watcher/
│       ├── Dockerfile
│       ├── instructions.md
│       ├── main.py                     ← uses RetryGate; no git, no model calls
│       ├── ci_status.py
│       └── retry_gate.py               ← logic-only; no model calls, no file edits, no git
├── scripts/
│   ├── bootstrap_foundry_project.sh
│   └── update_agent.py
├── tests/
│   ├── test_nexus_client.py
│   ├── test_code_fixer.py
│   └── test_retry_gate.py
└── .github/workflows/
    └── ci.yml
```

### Why this split

- **`infra/main.bicep`** — declares the Azure resources that must exist before any agent can run: ACR (container images), Key Vault (PAT \+ Nexus IQ key), and Cosmos DB Serverless account with the `oss-remediation` database and `tracking-records` container (90-day TTL, partition key `/id`). Does not provision the Foundry project itself — only references an existing one.  
    
- **`infra/agents/*.agent.yaml`** — Hosted Agent definition files (name, container image reference, cron schedule, env vars, Key Vault secret references, identity scopes). The Watcher yaml adds `COSMOS_ENDPOINT`, `COSMOS_DATABASE`, `COSMOS_CONTAINER`, and `MAX_RETRY_ATTEMPTS`; the Fixer yaml adds `NEXUS_IQ_APP_PUBLIC_ID`, `GITHUB_REPO_TARGET`, and `MODEL_DEPLOYMENT_NAME`.  
    
- **`agents/common/`** — a shared Python package copied into all container images at build time (see Dockerfiles). No agent has a runtime dependency on another's container. Contains the tracking store abstraction (all agents read/write) and the knowledge store abstraction (Classifier, Knowledge Init, and Fixer read/write KB entries).  
    
  - **`tracking_store.py`** — single source of truth for all fix attempt state. Contains `TrackingRecord` dataclass, `TrackingStatus` enum, `CosmosTrackingStore` (production) and `InMemoryTrackingStore` (testing) backends, and the `make_fresh_record` / `make_retry_record` factory functions. The `make_retry_record` function is called exclusively by the Watcher; it sets `status=RETRY_REQUESTED` and stores `failure_log_excerpt` before the Fixer is invoked.


- **`agents/discovery/`** _(deferred — not yet built)_ — planned as the entry point for every scan run. Will read the Nexus IQ report and immediately apply `ignore_list.yaml` and `known_list.yaml` against each finding. Any matching finding gets a GitHub Issue (reason: accepted risk, blocked upgrade, EOL) and is written as an `IGNORED` or `KNOWN_BLOCKED` tracking record, then dropped from further processing. No model calls, no KB access, no classification. **Current state:** the Fixer's `_run_fresh_scan()` fetches the Nexus report directly and passes all findings to the Knowledge Agent. Findings that would be filtered by Discovery instead land in the Classifier, where Bucket 1 catches the "no safe version" case as a triage issue. The ignore/known-list pre-filter — which avoids spending KB tokens on accepted-risk findings — is the gap. Discovery becomes valuable once the customer provides their first ignore/known lists. Until then, all findings flow through the full pipeline harmlessly.

- **`agents/knowledge_agent/`** — runs after Discovery, for every actionable finding. For each `(component, old_version, new_version)` triple, checks whether a KB entry already exists; if not, queries official release notes and migration guides via web search scoped to trusted domains, parses the results into structured migration intelligence (removed APIs, changed signatures, new imports), and writes the entry to the Knowledge Store. A no-op if the KB already has an entry for that version pair. Runs as a lightweight container with no Maven and no git.

- **`agents/classifier/`** — runs after KB hydration, not before. This is where actual bucket assignment happens, and it happens with KB data available. `rules.py` reads the KB entry for each finding and applies classification rules: Bucket 1 (no safe path) is detectable without KB; Buckets 3 and 4 (major with vs. without known migration path) are only distinguishable once the KB has been queried. Invokes the Fixer for fixable findings (Buckets 2 and 3). Writes bucket + rationale to the tracking record.

  GitHub Issue creation differs by bucket type:
  - **Bucket 1** (no safe path — no Nexus remediation target, transitive dep, EOL): Issue created without a model call. The reason is derivable directly from Nexus data; no KB content is needed.
  - **Bucket 4** (complex/framework-level): Issue content is Claude-generated. The Classifier reads the KB entry for this finding — which the Knowledge Agent hydrated from web sources — and asks Claude to produce a structured analysis: known breaking changes, what coordination across dependencies is required, and suggested starting points for the engineer. The Issue contains this analysis, not a bare "too complex" message. Framework-level upgrades (Spring Boot, Angular major, Hibernate) always land here regardless of KB content — the KB analysis is surfaced in the Issue but the Fixer is never invoked.

- **`agents/fixer/`** — the only component that ever writes code or pushes git commits. `main.py` has two trigger modes selected at startup by the `RETRY_TRACKING_ID` env var: fresh scan (scheduler-triggered) and Watcher-triggered retry. Fresh scans run all findings in parallel via `ThreadPoolExecutor` (up to `MAX_PARALLEL_FIXES = 5` concurrent workers): the repo is cloned once from GitHub, then each worker gets a fast local copy via `clone_local()` — which uses filesystem hardlinks and re-points `origin` to GitHub for push. Tracking records are created before the pool starts so store access stays sequential. `code_fixer.py` exposes two public entry points (`run_fresh_fix`, `run_retry_fix`) and raises `InvalidRetryError` if the tracking record fails validation — the Fixer's own second line of defence against being invoked improperly. On startup, `detect_framework(repo_path)` inspects the repository root and returns the appropriate `BuildFramework` implementation; all subsequent build and test calls go through this abstraction. The tool-use loop exposes **five tools** (up to `MAX_TOOL_ROUNDS = 10`): `grep_files`, `read_file`, `apply_file_change` (same as before), plus `run_build` (delegates to the detected framework's compile/typecheck command) and `run_unit_tests` (delegates to the framework's unit-only test command). The PR is opened only after `run_build` returns SUCCESS and `run_unit_tests` returns SUCCESS or NO_TESTS_FOUND. Prompt templates (`FRESH_FIX_PROMPT`, `RETRY_FIX_PROMPT`) are module-level strings, not buried logic, so they diff cleanly in PRs. The Fixer Docker image installs all supported runtimes (JDK 17 + Maven, Node 20 + npm, Python 3.11 + pip); the Watcher and Classifier images remain lightweight.  
    
- **`agents/watcher/`** — the only component that polls CI and decides whether to retry. `retry_gate.py` contains zero model calls, zero file edits, and zero git operations. It checks the retry bound, creates a `RETRY_REQUESTED` tracking record with the CI failure excerpt, and invokes the Fixer container. It never invokes the Fixer if the bound is reached. `main.py` imports no git library; the Watcher container image does not need the `git` binary installed.  
    
- **`streamlit_dashboard.py`** — a read-only, local-only Streamlit app (OBS-01). Connects to the tracking store (Cosmos if `COSMOS_ENDPOINT` is set, InMemory otherwise), and renders three views: run history table, retry lineage drill-down by PR, and POC success metrics (resolution rate, avg/p50/p95 time-to-resolution, token usage). No hosting story required for the POC; any team member runs it on their own laptop with `az login` and an appropriate Cosmos RBAC assignment.  
    
- **`scripts/bootstrap_foundry_project.sh`** — run **once**. Deploys Bicep, captures outputs (ACR login server, Key Vault URI, Cosmos endpoint) to `.env.infra`, and prints the manual RBAC grant commands including the Cosmos DB Built-in Data Contributor role for each agent managed identity.  
    
- **`scripts/update_agent.py`** — run **every time** a change is shipped. Builds and pushes container images (tagged with git SHA), then calls the Foundry SDK to create-or-update each Hosted Agent definition. The Fixer image build must verify that `mvn --version` succeeds as a build-time smoke test so a missing Maven installation fails the build, not runtime.  
    
- **`tests/`** — unit tests for everything testable without AAF access. Each test module has isolated fixtures so a schema change in `TrackingRecord` requires updating exactly one place. Test names describe safety properties rather than method names (e.g. `test_fixer_refuses_when_status_is_not_retry_requested`, `test_apply_file_change_returns_error_not_silent_skip_when_find_string_absent`, `test_compile_failure_surfaces_stderr_not_exception`).  
    
- **`.github/workflows/ci.yml`** — CI for this repo (lint \+ test our orchestration code), separate from the customer's own CI pipeline that the Fixer agent's PRs will trigger.

---

## 4\. End-to-End Flow

```
   ╔══════════════════════════════════════════════════════════════════╗
   ║   DISCOVERY AGENT — DEFERRED (post-POC)                         ║
   ║   Will pre-filter findings via ignore_list.yaml / known_list.yaml║
   ║   writing IGNORED / KNOWN_BLOCKED records and skipping those     ║
   ║   findings before KB tokens are spent on them.                   ║
   ║   Current state: Fixer fetches Nexus report directly; all        ║
   ║   findings pass to the Knowledge Agent unfiltered.               ║
   ╚══════════════════════════════════════════════════════════════════╝
                                   │
                                   ▼ (all findings — no pre-filter until Discovery is built)
   ┌──────────────────────────────────────────────────────────────────┐
   │         KNOWLEDGE AGENT (runs per finding, inside Fixer startup)  │
   │                                                                    │
   │  For each (component, old_version, new_version):                  │
   │    • Check Knowledge Store — if entry exists, skip (no-op)       │
   │    • Web-search official release notes + migration guides         │
   │      scoped to trusted domains                                    │
   │    • Parse: removed APIs, renamed methods, new imports, patterns  │
   │    • Persist structured entry to Knowledge Store                  │
   └──────────────────────────────────────────────────────────────────┘
                                   │
                                   ▼ (KB now hydrated)
   ┌──────────────────────────────────────────────────────────────────┐
   │         CLASSIFIER AGENT (runs after KB; classification is        │
   │         KB-informed — buckets 3/4 require KB entry to distinguish)│
   │                                                                    │
   │  For each finding, read KB entry + apply rules:                  │
   │    Bucket 1 — No safe upgrade path                               │
   │               → create Issue; write SKIPPED record; stop         │
   │    Bucket 2 — Patch / Minor (no breaking changes in KB)          │
   │               → invoke Fixer                                      │
   │    Bucket 3 — Major with known migration path (KB has data)      │
   │               → invoke Fixer (KB context injected into prompt)   │
   │    Bucket 4 — Complex / framework-level, no KB migration path    │
   │               → create Issue with KB analysis; skip Fixer        │
   │  Write bucket + rationale to each finding's tracking record      │
   └──────────────────────────────────────────────────────────────────┘
                                   │
                                   ▼ (Buckets 2 and 3 only)
   ┌──────────────────────────────────────────────────────────────────┐
   │              FIXER AGENT — Entry point A (scheduler trigger)      │
   │                  RETRY_TRACKING_ID env var is NOT set             │
   │                                                                    │
   │  1. Read bucket from tracking record (set by Classifier after KB) │
   │  2. Check Knowledge Store for (component, old_version, new_version)│
   │     KB hit (Tier 1 learned pattern or Knowledge Agent entry):    │
   │       → apply stored find/replace pairs via apply_file_change     │
   │       → run_build + run_unit_tests; if both pass → skip LLM call │
   │       → if FAILURE → fall back to Claude loop with KB injected   │
   │  3. Repo cloned once; a runs sequentially, b–g run in parallel   │
   │     (ThreadPoolExecutor, up to MAX_PARALLEL_FIXES=5 workers):    │
   │       a. Create a CREATED tracking record in the tracking store   │
   │       b. clone_local() from shared source (hardlinks, ~0.4 s);   │
   │          create remediation branch (skip if already exists)       │
   │       c. Bump dependency in pom.xml via XML parser (never         │
   │          string-replace); this happens before the loop starts     │
   │       d. Call Claude via tool-use loop (FRESH_FIX_PROMPT):        │
   │            Claude: grep_files → read_file → apply_file_change     │
   │                    (writes to disk immediately; ERROR if find      │
   │                     string not exact match → Claude re-reads)     │
   │                 → run_build (framework-detected compile/typecheck) │
   │                    SUCCESS → run_unit_tests (unit-only, no ITs)   │
   │                      SUCCESS / NO_TESTS_FOUND → end_turn          │
   │                      FAILURE → Claude reads test output,          │
   │                                self-corrects, re-runs both gates  │
   │                    FAILURE → Claude reads STDERR, self-corrects,  │
   │                              apply_file_change + run_build again  │
   │       e. Commit + push branch (only after both gates pass)        │
   │       f. Open PR — main thread, as each future resolves           │
   │          (idempotent: skips if open PR already exists)            │
   │       g. Update tracking record: PR_OPENED → CI_PENDING           │
   │          Record pr_number, branch_name, token_usage               │
   └──────────────────────────────────────────────────────────────────┘
                                   │
                                   ▼
                   customer's CI pipeline runs automatically
                        (unit tests + integration tests)
                                   │
                                   ▼
   ┌──────────────────────────────────────────────────────────────────┐
   │              WATCHER AGENT (every 15 minutes, no git access)      │
   │                                                                    │
   │  For each open remediation PR (branch prefix "fix/"):             │
   │  1. Load latest tracking record from tracking store               │
   │  2. Skip if status is CI_PASSED / FAILED_MAX_RETRIES / ESCALATED  │
   │  3. Poll CI status via GitHub Checks API (wait_for_ci timeout)    │
   │                                                                    │
   │  CI passed → update record to CI_PASSED; leave for human review   │
   │                                                                    │
   │  CI timed out → log warning; retry on next 15-minute cycle        │
   │                                                                    │
   │  CI failed (RetryGate.process_ci_failure):                        │
   │    → Update record to CI_FAILED                                   │
   │    → Count all attempts for this PR in tracking store             │
   │    → If count >= MAX_RETRY_ATTEMPTS:                              │
   │         Write FAILED_MAX_RETRIES on record                        │
   │         Post escalation comment on PR (human review required)     │
   │         STOP — Fixer is never invoked again for this PR           │
   │    → Otherwise:                                                   │
   │         Create child tracking record:                             │
   │           status          = RETRY_REQUESTED                       │
   │           failure_log_excerpt = CI log text (truncated to 4 000   │
   │                                 chars) — so the retry is INFORMED  │
   │           parent_tracking_id = previous attempt's tracking_id     │
   │           attempt_number  = previous + 1                          │
   │         Set RETRY_TRACKING_ID = new record's tracking_id          │
   │         Invoke Fixer container via AAF SDK (AafFixerInvoker)      │
   │         ↓ Fixer's Entry point B runs (see below)                  │
   └──────────────────────────────────────────────────────────────────┘
                                   │
                                   ▼
   ┌──────────────────────────────────────────────────────────────────┐
   │              FIXER AGENT — Entry point B (Watcher-triggered)      │
   │                  RETRY_TRACKING_ID env var IS set                  │
   │                                                                    │
   │  1. Read RETRY_TRACKING_ID from env                               │
   │  2. Validate tracking record (raises InvalidRetryError if any     │
   │     check fails — Fixer refuses to act):                          │
   │       • record exists in tracking store                           │
   │       • status == RETRY_REQUESTED (only Watcher sets this)        │
   │       • attempt_number <= MAX_RETRY_ATTEMPTS                      │
   │  3. Check out the EXISTING PR branch — never creates a new branch │
   │  4. Call Claude via tool-use loop (RETRY_FIX_PROMPT):             │
   │       failure_log_excerpt at top of prompt (informed retry)       │
   │       Claude: grep_files → read_file → apply_file_change          │
   │               (writes to disk immediately; ERROR on mismatch)     │
   │            → run_build (framework-detected compile/typecheck)      │
   │               SUCCESS → run_unit_tests (unit-only, no ITs)        │
   │                 SUCCESS / NO_TESTS_FOUND → end_turn               │
   │                 FAILURE → Claude self-corrects, re-runs both      │
   │               FAILURE → Claude reads STDERR, self-corrects,       │
   │                          apply_file_change + run_build again       │
   │  5. Commit + push to same branch (only after both gates pass)     │
   │     CI re-runs automatically on the new commit                    │
   │  6. Update tracking record: CI_PENDING                            │
   │     Record token_usage for this attempt                           │
   └──────────────────────────────────────────────────────────────────┘
                                   │
                                   └── CI re-runs → Watcher polls again → loop
```

---

## 4.4a. Deployment Mode Configuration

A single env var, `DEPLOYMENT_MODE`, controls which backend implementations are used for
both the tracking store and the knowledge store. This allows the same container images to
run locally (for testing and development) and in Azure AI Foundry (production) without
any code changes.

| `DEPLOYMENT_MODE` | Tracking store | KB store | When to use |
| :---- | :---- | :---- | :---- |
| `azure` | `CosmosTrackingStore` | `CosmosKBStore` | AAF production; also local testing against real Cosmos |
| `local` | `InMemoryTrackingStore` | `InMemoryKBStore` | Local development, CI unit tests; suppresses "unset" warning |
| _(unset)_ | Cosmos if `COSMOS_ENDPOINT` is set, else InMemory | Same logic | Backwards-compatible; warns if neither is set |

`COSMOS_ENDPOINT` can override mode detection: if `DEPLOYMENT_MODE` is unset but `COSMOS_ENDPOINT`
is present, both factories default to the Cosmos backend. Set `DEPLOYMENT_MODE=local` explicitly
to run InMemory even when `COSMOS_ENDPOINT` is set (useful for isolated unit tests).

### Cosmos containers

The Bicep template provisions three containers in the `oss-remediation` database:

| Container | Used by | Purpose | TTL |
| :---- | :---- | :---- | :---- |
| `tracking-records` | `CosmosTrackingStore` | One document per fix attempt; full `TrackingRecord` | 90 days |
| `kb-entries` | `CosmosKBStore` | Knowledge base entries (all three tiers) | None |
| `retry-attempts` | `CosmosAttemptCounter` (legacy) | Simple per-PR attempt counter; superseded by `count_attempts_for_pr()` | 90 days |

The `kb-entries` container env var override is `COSMOS_KB_CONTAINER` (default `kb-entries`).
The `tracking-records` container env var override is `COSMOS_CONTAINER` (default `tracking-records`).

---

## 4.5. Tracking Store — Schema and Dual-Enforcement Architecture

The tracking store is the shared state layer that makes the Fixer/Watcher role separation work safely across two separate containers that never share memory. Retry state lives in Cosmos DB, not in either agent's process — so a container restart loses nothing, and both agents always read the same ground truth.

### TrackingRecord schema

```
tracking_id          str      UUID, unique per attempt (not per PR)
vulnerability_id     str      CVE ID or component identifier
repo                 str      "org/repo"
component_name       str
old_version          str
new_version          str
status               str      TrackingStatus value (see below)
created_at           str      ISO 8601 UTC
updated_at           str      ISO 8601 UTC
parent_tracking_id   str?     None for first attempt; points to previous attempt's ID
pr_number            int?     None until Fixer creates the PR
branch_name          str      Set by Fixer on fresh scan; inherited by retry records
attempt_number       int      1 for first attempt; incremented per retry
time_to_resolution_seconds  float?  Set at CI_PASSED or FAILED_MAX_RETRIES
token_usage          dict?    {"prompt_tokens": N, "completion_tokens": N}
failure_log_excerpt  str?     Written by Watcher; read by Fixer on retry (max 4 000 chars)
framework_detected   str?     e.g. "maven", "gradle", "npm", "python"; set by Fixer at startup
unit_test_status     str?     "SUCCESS" | "NO_TESTS_FOUND" | "SOFT_FAIL" — set at end_turn
bucket               int?     1–4; set by Classifier after KB hydration
skip_reason          str?     Set by Classifier (Bucket 1/4) or, once built, Discovery (ignore/known-list match); included in GitHub Issue body
```

### Status state machine

```
IGNORED          (Discovery: matched ignore_list.yaml — terminal, Issue created) [DEFERRED]
KNOWN_BLOCKED    (Discovery: matched known_list.yaml — terminal, Issue created)  [DEFERRED]
SKIPPED          (Classifier: Bucket 1/4 — no fix path — terminal, Issue created)

CREATED → PR_OPENED → CI_PENDING → CI_PASSED             (happy path)
                                 → CI_FAILED → RETRY_REQUESTED  (Watcher gates)
                                             → FAILED_MAX_RETRIES
                                             → ESCALATED
```

`RETRY_REQUESTED` is the only status that permits the Fixer's retry entry point to act. Only the Watcher sets it; the Fixer only reads it to validate. `IGNORED` and `KNOWN_BLOCKED` are reserved for the Discovery agent (deferred); they are defined in the schema but no current code sets them. `SKIPPED` is active — the Classifier sets it for Bucket 1/4 findings.

### Dual-enforcement safety model

The retry bound is enforced at two independent layers, so neither agent can bypass it alone:

| Layer | Where | Mechanism |
| :---- | :---- | :---- |
| Primary gate | Watcher (`retry_gate.py`) | Counts `attempt_count = tracking_store.count_attempts_for_pr(pr_number)` before creating any `RETRY_REQUESTED` record. If `count >= MAX_RETRY_ATTEMPTS`, writes `FAILED_MAX_RETRIES` and stops — never invokes Fixer. |
| Secondary guard | Fixer (`code_fixer.py`) | `run_retry_fix()` reads the tracking record and raises `InvalidRetryError` if `status != RETRY_REQUESTED` or `attempt_number > MAX_RETRY_ATTEMPTS`. Fixer refuses to act and exits non-zero. |

This means even if the Watcher were misconfigured or called externally, the Fixer would independently reject a retry that exceeds the bound or lacks a valid `RETRY_REQUESTED` record.

---

## 4.6. Autonomy Guarantees — Why No Human Step is Required Before PR Creation

The system is designed to run completely unattended from Nexus scan to open PR. Four architectural properties enforce this end-to-end:

### 1\. Tool execution is pure Python or managed subprocess — no shell, no approval prompt

When Claude calls any of the four tools during the tool-use loop, the call is handled entirely within the container process:

```
Claude API response: stop_reason="tool_use", block.name="run_build"
        │
        ▼
_dispatch_tool()          ← Python method call inside the container
        │
        ├── grep_files        → re.compile() + Path.rglob() + file.read_text()
        │                        No subprocess. No shell binary.
        │
        ├── read_file         → Path.read_text(path), truncated at 50k chars
        │                        No subprocess. No shell binary.
        │
        ├── apply_file_change → Path.read_text() + content.replace(find, replace, 1)
        │                        + Path.write_text()
        │                        Returns ERROR string if find not in content.
        │                        No subprocess. No shell binary.
        │
        ├── run_build         → framework.build(repo_path)
        │                        Dispatches to detected BuildFramework implementation:
        │                          Maven  → subprocess.run(['mvn','compile','-q','--batch-mode'], timeout=300)
        │                          Gradle → subprocess.run(['./gradlew','classes','-q'], timeout=300)
        │                          npm    → subprocess.run(['npm','run','build','--if-present'], timeout=300)
        │                          Python → subprocess.run(['python','-m','compileall','.'], timeout=60)
        │                        Returns "build: SUCCESS" or STDERR text.
        │
        └── run_unit_tests    → framework.test_unit(repo_path)
                                 Dispatches to detected BuildFramework implementation:
                                   Maven  → subprocess.run(['mvn','test','-q',
                                              '-Dexclude=**/*IT.java,**/*IntegrationTest.java'], timeout=600)
                                   Gradle → subprocess.run(['./gradlew','test','-q'], timeout=600)
                                   npm    → subprocess.run(['npm','test','--','--testPathIgnorePatterns=integration'],
                                              timeout=600, env={**os.environ, 'CI':'true'})
                                   Python → subprocess.run(['pytest','-q','-m','not integration',
                                              '--ignore=tests/integration'], timeout=600)
                                 Returns "tests: SUCCESS", "tests: NO_TESTS_FOUND", or failure output.
                                 Integration tests are structurally excluded per framework — they require
                                 external systems (DB, messaging) that are not available in the container.
```

This is structurally different from Claude Code CLI (where Claude asks your local shell to run commands and you can be prompted for approval). Here, the Anthropic API returns a JSON block describing what it wants; the Python code fulfills it directly. Nothing pauses for a human.

### 2\. In-loop apply eliminates silent no-ops that would require human triage

**Before** (post-loop batch apply): Claude returned a `files_to_change` list at `end_turn` with `find`/`replace` strings that Python applied after the loop. If a `find` string didn't match the actual file content, `_apply_file_change()` silently returned `False` and skipped the file. The fix appeared to succeed, but no change was made — CI would fail for an opaque reason, requiring a human to investigate why the PR "fixed nothing."

**After** (in-loop `apply_file_change` tool): Claude calls `apply_file_change` during the loop. If the `find` string is not an exact substring of the current file content, the tool returns an ERROR string to Claude in the same conversation turn. Claude sees the error, calls `read_file` to get the actual current content, and retries with a corrected `find` string — all within the same `_call_model()` invocation, before `end_turn`. Silent partial-application is structurally prevented.

### 3\. In-loop compile gate catches API-breakage before CI

**Before** (no local verification): A version upgrade from e.g. `okhttp 3.x` to `4.x` changes method signatures. Claude would apply the change, the loop would end, and CI would fail with a compilation error. The Watcher would need to trigger a retry, consuming another Claude API call and another CI run just to catch what `mvn compile` would have flagged in seconds.

**After** (`run_maven_compile` before `end_turn`): Claude calls `run_maven_compile` after applying changes. On SUCCESS it calls `end_turn`. On FAILURE it receives the compiler STDERR, reads the affected files with `read_file`, applies corrections with `apply_file_change`, and compiles again — all within the same loop. The PR is only opened after `mvn compile -q` passes. This eliminates CI round-trips for the most common class of post-upgrade failures (deprecated API calls, renamed classes, changed method signatures) without running the test suite.

### 4\. Clone-once \+ parallel workers eliminates the sequential per-finding bottleneck

**Before**: `for finding in findings` called `repo.clone(github_url)` inside the loop — a full GitHub network round-trip per vulnerability, fully sequential. 10 findings ≈ 80 s of clone overhead before the first fix started. Claude API calls (\~5–15 s each) then ran one at a time.

**After**:

- One GitHub clone up front (`source_repo.clone()`).  
- Each worker calls `clone_local(source_path)` — filesystem hardlinks, \~0.4 s, no GitHub rate-limit exposure per finding.  
- `ThreadPoolExecutor(max_workers=MAX_PARALLEL_FIXES)` fans out all Claude API calls concurrently. PR creation and tracking store writes run in the main thread via `as_completed()` — sequential and race-free.  
- 10 findings: \~80 s (before) → \~11 s clone overhead \+ \~10–15 s for two parallel Claude rounds \= \~25 s total.

### Human gates — where they correctly exist

| Gate | Mechanism | Where it lives |
| :---- | :---- | :---- |
| PR merge | GitHub branch protection / required reviewers | GitHub settings, not our code |
| Max-retry escalation | `FAILED_MAX_RETRIES` written; PR comment posted; agent exits | `retry_gate.py:_handle_limit_reached()` — notifies, does not block |
| Fixer invocation failure | `ESCALATED` written; PR comment posted | `retry_gate.py:process_ci_failure()` exception handler |

No human action is required or expected at any earlier point. A human step before PR creation would be a bug, not a guardrail.

---

## 4.7. Framework Abstraction — Supported Tech Stacks

### `BuildFramework` interface

`agents/fixer/frameworks/__init__.py` declares a `BuildFramework` abstract base class with four methods. All framework implementations must satisfy this interface:

```python
class BuildFramework(ABC):
    @classmethod
    def detect(cls, repo_path: Path) -> bool: ...   # True if this framework owns this repo
    def bump_dependency(self, repo_path, component, old_ver, new_ver) -> None: ...
    def build(self, repo_path) -> BuildResult: ...       # compile / typecheck
    def test_unit(self, repo_path) -> TestResult: ...    # unit tests only; ITs excluded
```

`detect_framework(repo_path)` tries each implementation in priority order and returns the first match. Priority order matters for polyglot repos (e.g. a Java project with a `package.json` for frontend tooling should resolve as Maven, not npm):

```
1. pom.xml present            → Maven
2. build.gradle / *.kts       → Gradle
3. package.json present        → npm  (covers Node, React, Angular — build script differs, test command same)
4. requirements.txt / pyproject.toml / setup.py → Python
```

If no framework is detected, `run_build` returns `"ERROR: no supported build file found"` and Claude proceeds to `end_turn` without a compile gate (degraded mode — CI is the fallback).

### Per-framework implementation

| Framework | `bump_dependency` | `build` command | `test_unit` command | IT exclusion mechanism |
| :---- | :---- | :---- | :---- | :---- |
| **Maven** | XML parser (`xml.etree`) on `pom.xml` | `mvn compile -q --batch-mode` (300 s) | `mvn test -q --batch-mode -Dexclude="**/*IT.java,**/*IntegrationTest.java"` (600 s) | Surefire plugin runs UTs; Failsafe plugin runs ITs — invoking `mvn test` (not `verify`) never triggers Failsafe |
| **Gradle** | Regex on `build.gradle` / TOML parse for `libs.versions.toml` | `./gradlew classes -q` (300 s) | `./gradlew test -q` (600 s) | By convention, Gradle's `test` task runs unit tests; integration test tasks are named separately (`integrationTest`, `itest`) |
| **npm** | `json.loads` / `json.dumps` on `package.json` | `npm run build --if-present` (300 s); falls back to `npx tsc --noEmit` if no build script | `CI=true npm test -- --testPathIgnorePatterns=integration --watchAll=false` (600 s) | `CI=true` prevents interactive watch mode; `--testPathIgnorePatterns` excludes paths matching `integration` |
| **Python** | `re.sub` on `requirements.txt`; TOML parse for `pyproject.toml` | `python -m compileall . -q` (60 s) | `pytest -q -m "not integration" --ignore=tests/integration` (600 s) | `pytest -m` respects `@pytest.mark.integration` markers; `--ignore` excludes the conventional integration test directory |

### Docker strategy

All four runtimes are installed in a single Fixer Docker image. This makes the image larger (~1.5 GB) but avoids per-framework container definitions and keeps the framework detection fully runtime-driven:

```dockerfile
FROM ubuntu:22.04
RUN apt-get install -y openjdk-17-jdk maven nodejs npm python3.11 python3-pip
```

Gradle does not require a global install — projects include a `gradlew` wrapper; the Fixer calls `./gradlew` relative to the repo root. If `gradlew` is absent, `build()` returns an error and degrades gracefully.

For production, framework-specific images reduce attack surface and image size. This is a post-POC concern.

---

## 4.8. In-Loop Build and Unit Test Verification — Design Decisions

### What the two-gate sequence catches vs. what CI still covers

| Failure class | Caught by `run_build`? | Caught by `run_unit_tests`? | Still requires CI? |
| :---- | :---- | :---- | :---- |
| Deprecated method/class removed in new version | Yes | — | No |
| Method signature change (parameter type/count) | Yes | — | No |
| Missing import after package restructure | Yes | — | No |
| Unit test broken by changed API behaviour | No | Yes (if test exists) | No |
| Semantic / behavioural regression not covered by unit tests | No | No | Yes |
| Integration test failure (DB, messaging, env-specific) | No | No | Yes |
| Runtime exception (wrong config, env-specific) | No | No | Yes |

The compile gate eliminates the most common class of post-upgrade failure (API breakage). The unit test gate catches a second class (behavioural regressions that unit tests already cover). Integration tests and semantic regressions are correctly left to the customer's CI pipeline.

### Unit test gate: soft gate, not hard gate

Unit tests are a **soft gate**. When `run_unit_tests` returns a failure, Claude receives the test output as a `tool_result` and self-corrects (same mechanism as compile failures). If tests are still failing after `MAX_TOOL_ROUNDS`, the PR is opened anyway with `unit_test_status: SOFT_FAIL` recorded in the tracking record and a note in the PR description explaining which tests failed and why. The customer's CI pipeline will catch this and the Watcher retry loop handles it.

A hard gate (blocking PR until all unit tests pass) risks two failure modes that are unacceptable for an autonomous system:

- **Pre-existing failures**: If a test was already broken before the dependency change, blocking on it conflates an unrelated pre-existing issue with the fix being attempted. The agent cannot distinguish this without running a baseline test against the unpatched repo, which doubles runtime and complexity for every finding.
- **Flaky tests**: A test that fails intermittently would block PR creation non-deterministically, requiring human triage of a failure the agent caused only by running the test.

The soft gate captures the value (Claude self-corrects on genuine test failures it caused) without the blocking risk. The `unit_test_status` field in the tracking record gives full visibility into test state at PR creation time.

### Design choices

**`_bump_pom_version` handles three real-world pom.xml patterns.** The XML parser-based bump handles
cases beyond a simple `<version>X.Y.Z</version>` literal:

1. **Maven property references** — `<version>${spring.version}</version>` is common in Spring-family projects. The implementation resolves the property name and updates it in `<properties>`, falling back to inlining the version directly if the property element is not found.
2. **BOM-managed dependencies** — dependencies managed by a BOM import often have no `<version>` element. The implementation adds an explicit `<version>` override so the bump is applied regardless of what the BOM specifies.
3. **Bare pom.xml files** — some legacy or generated pom.xml files omit the Maven namespace (`xmlns="http://maven.apache.org/POM/4.0.0"`). The implementation detects whether the namespace is present and adapts XPath queries accordingly. Without this, namespace-prefixed queries silently return no matches against bare files.

All three cases raise `PomXMLError` with a specific message if the dependency is genuinely absent, which Claude receives as a tool result and diagnoses.

**`run_build` before `run_unit_tests`, never both at once.** Claude always runs `run_build` first. If it fails, Claude self-corrects before calling `run_unit_tests`. Running tests against code that doesn't compile wastes the test timeout (up to 600 s) and produces noise output. The two tools are always called in sequence within the loop.

**Claude self-corrects within the same loop.** When either gate returns a failure, Claude receives it as a `tool_result` in the same conversation and immediately applies corrections. This is not a new loop invocation — it is additional turns inside the same `_call_model()` call. Token usage is accumulated across all rounds and written to the tracking record at `end_turn`.

**Build output capped at 10 000 chars; test output capped at 20 000 chars.** Compiler output for a large project with cascading errors can be enormous. Test output for a suite with many failures is larger still. The caps ensure tool results fit comfortably within Claude's context window. Truncation is applied from the end (the first errors are most useful).

**Graceful degradation if the build tool is absent.** Each `build()` and `test_unit()` implementation catches `FileNotFoundError` from `subprocess.run` and returns an ERROR string. Claude sees the error, skips the gate, and calls `end_turn`. The PR is still opened. A misconfigured container degrades to no-gate behaviour rather than crashing.

**`NO_TESTS_FOUND` is a valid success.** If `test_unit()` finds no test files matching the framework's pattern, it returns `"tests: NO_TESTS_FOUND"` rather than failing. Claude treats this as equivalent to SUCCESS and calls `end_turn`. This handles repositories that have tests only at the integration level (all excluded by design) or genuinely have no test suite yet.

**Dependency manifest is bumped before the loop, never by Claude.** `bump_dependency()` (framework-specific) is called before `_call_model()`. Claude is explicitly instructed in both prompt templates not to edit the dependency manifest (`pom.xml`, `package.json`, `requirements.txt`). This ensures the bump is always applied correctly and that both gates always test against the already-bumped version.

### Files-changed tracking

`self._applied_changes: list[str]` is initialised to `[]` before each `_call_model()` call. Every successful `apply_file_change` call appends the relative path. After `end_turn`, `ChangeSummary.files_changed` is built as:

```py
[manifest_file] + list(dict.fromkeys(self._applied_changes))
```

where `manifest_file` is the framework's dependency manifest (`pom.xml`, `package.json`, etc.). `dict.fromkeys` deduplicates while preserving insertion order. The PR description and tracking record both receive this list, giving an accurate audit of what was changed without relying on Claude's self-report.

---

## 5\. What the AAF-Access Person Must Do

We hand them a repo. They need to do a small number of things that **require their access** and cannot be done or tested by us:

1. **Confirm/create the Foundry project** — needs subscription-level Owner/Contributor or Account Owner role. We are not creating the Foundry account/project in Bicep — only referencing an existing one. Provisioning a brand-new Foundry account is a one-time portal/CLI action on their side.  
     
2. **Deploy a model** (e.g., Claude via AAF's model catalog) inside that project's "Models \+ Endpoints" and give us the deployment name.  
     
3. **Grant RBAC for agents and scripts:**  
     
   - Their own identity (or the identity that runs scripts) needs Foundry/Azure AI User role at the project scope.  
   - Each Hosted Agent's managed identity needs Container Registry Repository Reader on the ACR.  
   - Each Hosted Agent's managed identity needs **Cosmos DB Built-in Data Contributor** (the data-plane role, not ARM Contributor) on the Cosmos DB account, so the tracking store can read and write records without connection strings.

```shell
# Run once per agent managed identity after bootstrap completes
az cosmosdb sql role assignment create \
  --account-name <cosmos-account-name> \
  --resource-group <rg> \
  --role-definition-name "Cosmos DB Built-in Data Contributor" \
  --principal-id <agent-managed-identity-object-id> \
  --scope "/"
```

4. **Populate Key Vault secrets:** the customer's GitHub PAT and Nexus IQ API key. We provision the empty Key Vault with RBAC authorization enabled; they put the actual secret values in. The Cosmos DB endpoint is passed as a plain env var (not a secret) — it is not sensitive.  
     
5. **Run our two scripts, in order:**  
     
   - `bootstrap_foundry_project.sh` — once. Deploys Bicep, outputs `.env.infra`.  
   - `update_agent.py` — every time there is a change to ship.

   

6. **Run the manual smoke test** (checklist in README) — trigger the Fixer agent once against a test repo, confirm a PR gets created with a tracking record and a passing `mvn compile` gate visible in the container logs, confirm the Watcher reacts to a deliberately broken CI run, confirm the retry bound stops after `MAX_RETRY_ATTEMPTS`.

Everything else — orchestration logic, Nexus parsing, retry/guardrail logic, tracking store, container builds — is built, tested (with mocks), and reviewed entirely on our side first.

---

## 6\. Open Item Before Building `nexus_client.py`

We have not yet confirmed: **which Nexus product and which API endpoint** the customer's existing scans actually use — Nexus IQ Server's policy/report API, or Nexus Repository's component API, or something else already wired up by whoever set up the current scans. `nexus_client.py` is written with `# FIXME` markers on every endpoint path and response field name. This must be resolved by asking the customer or reviewing Sonatype's current API docs before those markers can be replaced with real contract values.

---

## 7\. Observability & Visibility (POC)

### Decision

A read-only Streamlit dashboard (`streamlit_dashboard.py`, OBS-01) runs on the team laptop or a lightweight Azure Container Instance for demos. It connects to the tracking store and knowledge base via `make_tracking_store()` / `make_knowledge_store()`, selecting Cosmos (Azure) or InMemory (local) based on `DEPLOYMENT_MODE`.

The dashboard is co-branded for both **Neurealm** and **Premier Inc.** — a gradient header strip and CSS color system reflect both brands' identities. This makes the tool presentable to Premier stakeholders without additional design work.

### What it shows

| Tab | Content |
| :---- | :---- |
| **Run History** | All tracking records as a filterable table (by repo, status, component). Status icons (🟢🔴🟡⛔) at a glance. Sortable by `created_at`. |
| **Retry Lineage** | PR number selector → all attempts in order, each as an expandable card showing status, version change, token usage, and the CI failure excerpt that informed that retry. |
| **Metrics** | Resolution rate; avg/p50/p95 time-to-resolution (resolved PRs only); total and per-PR token spend; status distribution bar chart; retry depth distribution chart. |
| **Knowledge Base** | All KB entries across all three tiers. Tier-sorted, filterable by source. Each entry expands to show breaking changes, migration steps, find→replace patterns, and removed APIs. Tile summary shows entry counts per tier. |

### Empty-state and active-detection behaviour

Tabs render independently — the KB tab shows playbook entries even before the first tracking record exists, because the Knowledge Agent hydrates KB before any PR is opened. Each tab shows its own informative empty-state message rather than stopping page rendering.

When the fixer is processing (CREATED records exist), the page injects a `<meta http-equiv='refresh' content='10'>` so the page auto-reloads every 10 seconds without user action.

### How to run

```shell
# Install dependencies (once):
pip install streamlit pandas

# Azure (production data):
export DEPLOYMENT_MODE=azure
export COSMOS_ENDPOINT=https://<account>.documents.azure.com:443/
az login
streamlit run streamlit_dashboard.py

# Local (InMemory — playbook entries only, useful for layout checks):
export DEPLOYMENT_MODE=local
streamlit run streamlit_dashboard.py
```

The Cosmos role required is **Cosmos DB Built-in Data Reader** (read-only, not Data Contributor) since the dashboard never writes. This is a separate, narrower assignment from the one granted to the agent managed identities.

### Scope boundary

This is an internal team visibility tool for the POC, not a Premier-facing product. If a stakeholder demo requires a stable URL, the lightest lift is Azure Container Instances (`az container create`) — but that is a separate story.

---

## 8\. Next Steps

1. Confirm the Nexus IQ/Repository API question (Section 6\) and replace `# FIXME` markers in `nexus_client.py` with the real endpoint paths and response field names.  
2. Push the repo to GitHub so the AAF-access person can clone it.  
3. Hand the AAF-access person the README "Setup" section (Section 5, items 1–5).  
4. Run the manual smoke test together (Section 5, item 6\) — specifically verify the compile gate fires correctly by observing `run_maven_compile: SUCCESS` in the Fixer container logs before PR creation.  
5. Iterate based on what the smoke test surfaces, particularly around `AafFixerInvoker` SDK parameter names (Section 5, last row).
6. **Build `agents/discovery/` (post-POC hardening)** — implement once the customer provides their first `ignore_list.yaml` / `known_list.yaml`. Prerequisite: add `IGNORED` and `KNOWN_BLOCKED` status writes to `tracking_store.py` and create `config/` YAML files. The Fixer's `_run_fresh_scan()` will then pass the Nexus findings through `ListFilter` before handing them to the Knowledge Agent, eliminating KB tokens spent on accepted-risk findings.

---

## Post-POC: Customer Feedback — Questions & Resolutions

### Knowledge Base

**Customer concern:** The agent re-researches the same vulnerabilities on every run, wasting tokens and time. A centralized knowledge base of migration guides and version information would eliminate this.

**Resolution:** We will build a two-tier knowledge base, persisted in Azure Cosmos DB (`kb-entries` container) so entries survive container restarts and are shared across all agent instances. The KB is accessed via `make_knowledge_store()` which returns `CosmosKBStore` in Azure mode and `InMemoryKBStore` (preloaded with Tier 2 playbooks) in local mode.

**Tier 1 — Learned patterns (agent-written).** After a fix is confirmed by CI, the Watcher writes a KB entry containing: what files changed, the exact `find`/`replace` pairs applied per file, the model's rationale, and the token cost. On the next run where the same `(component, old_version, new_version)` tuple appears — in this repo or any other — the Fixer retrieves the stored patterns and applies them directly using `apply_file_change` and `run_maven_compile` without any LLM call. Only if direct pattern application fails (compile error, find-string mismatch against the new repo's actual content) does the agent fall back to a Claude API call with the KB entry injected as context. This eliminates the discovery phase entirely for known version pairs and reduces token cost to near-zero on repeat encounters.

When a fix exhausts its retry limit without succeeding, the KB records a negative entry for that version pair. This prevents the agent from blindly re-attempting the same failing upgrade on the next scheduled run.

**Tier 2 — Curated playbooks (engineer-written).** Tier 2 fills narrow, specific gaps where automated KB retrieval (Knowledge Initialization) cannot reliably produce complete or accurate migration guidance. It is not a mechanism for large-scale modernization — multi-major framework upgrades like Angular 14→17 are always Bucket 4 (create Issue, skip Fixer) regardless of whether a playbook exists.

Tier 2 targets three specific scenarios:

- **Customer-internal libraries with no public documentation.** The Knowledge Agent's web search returns nothing for a proprietary internal library. A Tier 2 playbook authored by the customer's engineers is the only way to provide migration guidance for something like `com.company.internal:auth-client 2→3`.
- **Customer-specific code patterns not present in any official guide.** The official migration guide says "replace `LogFactory.getLogger()`" but the codebase also wraps this in an internal utility class that the official docs will never mention. An engineer encodes that pattern once in a playbook; all repos that undergo the same upgrade benefit automatically.
- **Known major migrations where public docs exist but are fragmented or unreliable.** For something like log4j 1→2, the breaking changes are stable and well-understood, but the authoritative information is spread across multiple pages, GitHub issues, and blog posts. Automated parsing of that produces inconsistent results. An engineer authors a clean ordered set of steps once rather than relying on web scraping every time.

Engineers (or Neurealm as part of the engagement) author these playbooks as YAML files. The agent reads the relevant playbook before a major-version fix attempt, supplementing whatever the Knowledge Agent retrieved from web sources. Tier 2 and Knowledge Initialization are complementary inputs to the same KB lookup — Tier 2 guarantees coverage for the cases where automated retrieval isn't reliable enough.

**Knowledge Initialization (automated retrieval, runs before Fixer invocations).** For each identified CVE and its corresponding upgrade path (`old_version → safe_version`), a dedicated initialization step runs before any Fixer invocation. It queries authoritative external sources — official release notes, migration guides, changelogs — using web search scoped to trusted domains (e.g. `github.com/releases`, official library documentation sites). The retrieved content is parsed to extract structured migration intelligence: removed or renamed APIs, changed method signatures, new required imports, known breaking patterns. This structured output is persisted into the Knowledge Store against the `(component, old_version, new_version)` key before the Fixer runs.

When the Fixer subsequently processes a finding, it checks the Knowledge Store first. If a pre-hydrated entry exists, the Fixer injects it into the Claude prompt as grounded, version-specific migration context. This anchors Claude's suggestions to retrieved ground truth rather than parametric knowledge, which may be outdated for recently published versions, and materially reduces hallucination risk on version-specific API details. The distinction from Tier 1 is source: Tier 1 entries come from confirmed past fixes in production; Knowledge Initialization entries come from external documentation before any fix has been attempted.

**What this changes operationally:**

- First fix of any version pair: full model discovery, same cost as today.  
- Subsequent fixes of the same version pair: KB hit, materially lower token cost.  
- Major version migrations with a playbook: agent enters the fix loop with the breaking changes already known, higher success rate on first attempt.  
- The tracking record logs whether each run was a KB hit; the dashboard shows the cost reduction over time, quantifying the efficiency gain the customer asked about.

**What we need from the customer:**

- Confirmation of which repositories are in scope — KB entries can be shared across repos (e.g., any repo upgrading log4j benefits from a prior fix on another) or scoped per repo. This depends on whether the customer's repos share frameworks or are diverse.  
- A list of the most common vulnerable libraries in their Nexus backlog. We will pre-author Tier 2 playbooks for those libraries so the KB is useful from day one rather than requiring a cold-start period.

---

### Complex Upgrades vs. ROI

**Customer concern:** The agent only handles simple version bumps that engineers can already do easily. Major upgrades (e.g., Angular major version jumps) require far more context and testing. What is the ROI if the hard cases are out of scope?

**Resolution:** A dedicated **Classifier Agent** runs before any Fixer invocation and assigns every finding to exactly one of four buckets. Keeping classification in its own agent (rather than in the Fixer) means the Fixer's responsibilities stay narrow — it only fixes — and the classification logic can evolve independently (new library rules, updated thresholds) without touching the fix path. The Classifier writes its decision and rationale to the tracking record; the Fixer reads the bucket and either proceeds or exits immediately without any model call.

**Classification buckets:**

| Bucket | Trigger condition | Agent action |
| :---- | :---- | :---- |
| **1. No safe upgrade path** | Nexus reports no remediation target; transitive dep not in pom.xml; EOL with no maintained successor | Issue created immediately, no fix attempted |
| **2. Patch / Minor (auto-fix)** | Same major version, no playbook required | Fixer runs; Knowledge Init hydrates KB if web sources exist |
| **3. Major with knowledge** | Major version delta; Tier 1 KB hit or Tier 2 playbook exists or Knowledge Init retrieved context | Fixer runs with pre-hydrated context injected into prompt |
| **4. Complex major / framework-level** | Spring Boot, Hibernate, Angular, or other libraries on the "never attempt autonomously" list; major delta with no knowledge and no plan | Issue created with breaking-change analysis and steps needed; plan-gated: agent will retry when engineer provides a migration plan |

**What the agent handles autonomously:**

- **Patch and minor version bumps**: pom.xml bump plus targeted source changes for removed or renamed APIs. These are fully autonomous. For most Nexus backlogs this is the majority of findings by count.  
- **Known major version migrations** (with a KB playbook): If a Tier 2 playbook exists for the library, the agent can attempt the migration with higher confidence. log4j 1→2 is an example where the migration steps are well-documented and the agent can apply them reliably.

**What the agent does not attempt autonomously:**

- Major migrations without a playbook: the agent does not attempt blind major-version fixes.  
- Framework-level migrations (Spring Boot 2→3, Angular major): these require coordinated changes across many dependencies and are out of autonomous scope.  
- Cases where no safe upgrade path exists.

For anything outside autonomous scope, the agent creates a **GitHub Issue** (see Outlier Handling below) rather than attempting and failing.

**Where the ROI argument actually lives:** The customer framing — "if it only fixes easy things, what's the point?" — underestimates one key cost: *triage*. A Nexus report of 40 CVEs is an undifferentiated list. An engineer cannot tell at a glance which are 10-minute patch bumps and which are 2-week framework migrations. They triage manually, often discover the complexity only after starting, and spend significant time on context-gathering for cases they end up escalating anyway.

With the tiered system, every item in the backlog is classified in one agent run:

| Tier | Volume (typical) | Agent action | Engineer effort saved |
| :---- | :---- | :---- | :---- |
| Patch / Minor | 60–80% of CVE count | PR opened, CI verified, ready to merge | All of it — engineer only reviews the PR |
| Major (with playbook) | 10–15% | PR opened with higher success rate | Discovery and migration research |
| Major (no playbook) | 5–10% | Issue created with breaking-change analysis and steps needed | Triage and initial investigation |
| No path / Framework | 5–10% | Issue created with root cause and suggested action | Triage and context-gathering |

The agent does not just fix things — it processes the entire backlog and hands each item to engineers with the right level of context. Engineers spend their time on decisions and code review, not on triage and "what even needs to change here?"

**On human-provided migration plans:** For major migrations where a playbook doesn't yet exist, the agent creates an Issue that says: "This is a major version migration. Known breaking changes are X, Y, Z. Provide a migration plan and the agent will apply it on the next run." The engineer authors the plan once, and the agent applies it to every affected file.  This is the operational model for the "human-provided plans" the customer mentioned — the engineer provides the *what*, the agent does the *where* and *how across the codebase*.

**What we need from the customer:**

- Which libraries in their stack should be treated as framework-level (i.e., never attempted autonomously)? Spring Boot, Hibernate, Angular are common candidates but we need their specific list.  
- Are there internal or proprietary libraries in the Nexus scan results? These won't have public migration guides and may require a different approach.  
- Agreement on where the "attempt autonomously" boundary sits for major versions: always gate on plan presence first, or attempt once and fall back to an Issue on failure?

---

### Outlier Handling

**Customer concern:** When no upgrade path exists, the agent should not attempt unverified fixes. Instead, it should default to creating a GitHub Issue for human intervention.

**Resolution:** We will add explicit gates at two points in the flow. The gate decision is made before any fix is attempted where possible; where the outcome is only knowable after attempting, the gate fires on exhaustion.

**Gate 1 — Pre-attempt (before the Fixer runs):**

The following scenarios trigger an Issue immediately, with no fix attempt:

- No safe version exists in Nexus (the scan has no remediation target).  
- The vulnerable component is a transitive dependency — it is not declared directly in the project's `pom.xml` and cannot be bumped by the agent without broader dependency changes.  
- Complexity classification puts the finding above the autonomous capability threshold and no migration plan is present.  
- The component is end-of-life with no maintained upgrade path.

These Bucket 1 cases are detectable without any model call — the reason is derivable directly from Nexus data. The Issue is created at classification time.

Bucket 4 (complex/framework-level) also creates an Issue at classification time, but the Issue content is Claude-generated: the Classifier reads the KB entry hydrated by the Knowledge Agent and asks Claude to produce a structured analysis — known breaking changes, dependency coordination required, suggested starting points. The engineer receives a pre-analyzed artifact, not a bare "too complex" alert.

**Gate 2 — Post-attempt (after retries are exhausted):**

When a fix is attempted but CI fails beyond the retry limit, the current behavior posts a comment on the PR. This will be extended to also create a GitHub Issue containing:

- Why the fix failed — the root cause from the last CI failure log, not just "retries exhausted"  
- What was tried — which files changed, how many attempts, the failure pattern  
- Suggested next steps — derived from the KB playbook if available, or a structured prompt for the engineer to investigate

The PR comment becomes a brief pointer to the Issue; the Issue is the primary artifact.

**What a run with mixed results looks like:** If a scan produces 10 findings — 7 fixable, 3 outliers — the run opens 7 PRs and 3 GitHub Issues. These are separate, independent objects. Engineers can merge the PRs through normal review and track the Issues through their standard issue workflow. The agent does not mix unresolved outliers into the same PR as confirmed fixes.

**Audit trail:** Every finding — fixed or not — gets a tracking record. Outlier records carry a terminal status and a link to the GitHub Issue that was created. Over time the dashboard shows which vulnerabilities have been persistently unresolved: the same transitive CVE appearing in multiple repos, an EOL library with no replacement yet selected, a major migration without an available plan. This gives the customer a clear view of what the agent cannot touch and why, not just what it has fixed.

**What we need from the customer:**

- Where should Issues be created — in the same repository as the vulnerability, or in a central security/triage repository? A central repo is better if a security team monitors across multiple repos; per-repo is better if each repo's team owns its own backlog.  
- Should the agent close the PR when a post-attempt gate fires, or leave it open? Closing is cleaner (the Issue becomes the single artifact) but is irreversible without engineer action.  
- Should Issues be assigned automatically? If so, to which team or individual?

---

### How the Three Connect

These are not three independent features. The KB, Classifier Agent, Knowledge Initialization, and gates form a single loop:

The pipeline currently runs in three stages before the Fixer opens a PR (Discovery is deferred):

**Knowledge Agent → Classifier → Fixer**

_(Full four-stage design: **Discovery → Knowledge Agent → Classifier → Fixer** — Discovery will be inserted before the Knowledge Agent once the customer supplies ignore/known lists.)_

**Knowledge Agent** hydrates the KB from authoritative external sources for every finding — this is the data that makes the next step meaningful. It runs inline inside the Fixer's fresh-scan startup (not as a separate container yet). **Classifier** reads the KB entries and assigns buckets; critically, Buckets 3 and 4 are only distinguishable *after* KB hydration (a major version upgrade is Bucket 3 if the KB found a documented migration path, Bucket 4 if it didn't). The Classifier invokes the Fixer only for Buckets 2 and 3; Buckets 1 and 4 get triage GitHub Issues instead.

The **gate outcome** (CI pass, or retries exhausted) writes back to the **KB** as a Tier 1 learned-pattern entry via the Watcher's `PatternLearner`. Future runs for the same `(component, old_version, new_version)` triple get a KB hit at the Classifier stage — classification is instant, and the Fixer can apply stored patterns directly without a model call.

Build order: KB + Knowledge Store + Tracking Store infrastructure first, then Knowledge Agent (inline), then Classifier (inline), then the Fixer (KB-hit path + LLM fallback), then Watcher + retry logic + PatternLearner write-back, then Discovery as a hardening step once ignore/known lists exist. Each stage is independently deployable. The KB becomes more valuable as operational data accumulates — it does not need pre-population before anything works.  
