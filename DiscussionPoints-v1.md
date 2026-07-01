## End-to-End Flow

```
   ┌──────────────────────────────────────────────────────────────────┐
   │         DISCOVERY AGENT (no model calls, no classification)        │
   │                                                                    │
   │  1. Fetch latest Nexus IQ report for the target repo              │
   │  2. For each finding, check config/ignore_list.yaml:              │
   │       match → create GitHub Issue (reason: accepted risk);        │
   │               write IGNORED tracking record; skip finding         │
   │  3. For each remaining finding, check config/known_list.yaml:     │
   │       match → create GitHub Issue (reason: blocked upgrade);      │
   │               write KNOWN_BLOCKED tracking record; skip finding   │
   │  4. Output: filtered list of findings needing KB research         │
   └──────────────────────────────────────────────────────────────────┘
                                   │
                                   ▼ (actionable findings only)
   ┌──────────────────────────────────────────────────────────────────┐
   │         KNOWLEDGE AGENT (runs after Discovery, per finding)        │
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

## Post-POC: Customer Feedback — Questions & Resolutions

### Knowledge Base

**Customer concern:** The agent re-researches the same vulnerabilities on every run, wasting tokens and time. A centralized knowledge base of migration guides and version information would eliminate this.

**Resolution:** We will build a two-tier knowledge base.

**Tier 1 — Learned patterns (agent-written).** After a fix is confirmed by CI, the Watcher writes a KB entry containing: what files changed, the exact `find`/`replace` pairs applied per file, the model's rationale, and the token cost. On the next run where the same `(component, old_version, new_version)` tuple appears — in this repo or any other — the Fixer retrieves the stored patterns and applies them directly using `apply_file_change` and `run_maven_compile` without any LLM call. Only if direct pattern application fails (compile error, find-string mismatch against the new repo's actual content) does the agent fall back to a Claude API call with the KB entry injected as context. This eliminates the discovery phase entirely for known version pairs and reduces token cost to near-zero on repeat encounters.

When a fix exhausts its retry limit without succeeding, the KB records a negative entry for that version pair. This prevents the agent from blindly re-attempting the same failing upgrade on the next scheduled run.

**Tier 2 — Curated playbooks (engineer-written).** For major version migrations — log4j 1→2, Spring Boot 2→3, OkHttp 3→4 — the known breaking changes and migration steps are documented, stable, and not discoverable from the target repo alone. Engineers (or Neurealm as part of the engagement) author YAML playbooks for these libraries once. The agent reads the relevant playbook before any major-version fix attempt, regardless of whether it has seen that exact version pair before.

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

These are detectable without any model call. The Issue is created at classification time.

**Gate 2 — Post-attempt (after retries are exhausted):**

When a fix is attempted but CI fails beyond the retry limit, the current behavior posts a comment on the PR. This will be extended to also create a GitHub Issue containing:

- Why the fix failed — the root cause from the last CI failure log, not just "retries exhausted"  
- What was tried — which files changed, how many attempts, the failure pattern  
- Suggested next steps — derived from the KB playbook if available, or a structured prompt for the engineer to investigate

The PR comment becomes a brief pointer to the Issue; the Issue is the primary artifact.

**What a run with mixed results looks like:** If a scan produces 10 findings — 7 fixable, 3 outliers — the run opens 7 PRs and 3 GitHub Issues. These are separate, independent objects. Engineers can merge the PRs through normal review and track the Issues through their standard issue workflow. The agent does not mix unresolved outliers into the same PR as confirmed fixes.

**Audit trail:** Every finding — fixed or not — gets a tracking record. Outlier records carry a terminal status and a link to the GitHub Issue that was created. Over time the dashboard shows which vulnerabilities have been persistently unresolved: the same transitive CVE appearing in multiple repos, an EOL library with no replacement yet selected, a major migration without an available plan. This gives the customer a clear view of what the agent cannot touch and why, not just what it has fixed.
