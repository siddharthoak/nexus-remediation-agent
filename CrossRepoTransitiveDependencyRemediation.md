# Cross-Repo Transitive Dependency Remediation — Concept Explainer

## 1. Background: what this system does today

This project is an automated pipeline that fixes Open Source Software (OSS)
vulnerabilities in a company's code repositories. At a high level, two agents
work together:

- **The Fixer Agent** — given one repository and one vulnerability finding
  (a component name, its current vulnerable version, and a safe target
  version), it clones the repo, bumps the dependency version in the build
  manifest (for Java/Maven projects, that manifest is `pom.xml`), asks an AI
  model to fix any resulting code breakage (removed APIs, changed method
  signatures, etc.), verifies the fix compiles and passes unit tests, and
  opens a Pull Request (PR).
- **The Watcher Agent** — watches the PR's CI pipeline. If CI fails, it hands
  the failure logs back to the Fixer for another attempt, up to a bounded
  number of retries, before escalating to a human.

This works well when the vulnerable dependency is **declared directly** in
the repo being scanned — the Fixer can see the exact line in `pom.xml` that
needs a version bump.

## 2. The problem: transitive dependencies via parent POMs

Maven projects can declare a `<parent>` POM: instead of listing every
dependency and version directly, a project inherits settings — including
dependency versions — from a parent project's POM file. That parent project
is very often **a completely separate Git repository**, not a folder inside
the child project.

This creates a chain like:

```
Repo A (leaf application)
  <parent> → Repo B (shared "company-parent-pom")
                <parent> → Repo C (org-wide "base-parent-pom")
```

If a vulnerability scanner flags a component in Repo A, but the version of
that component is actually pinned inside Repo B's or Repo C's POM (via
`dependencyManagement` or a `<properties>` version variable), then:

- Repo A's own `pom.xml` **does not contain** the version string at all.
- Bumping anything in Repo A accomplishes nothing — the version is inherited,
  not declared locally.
- The real fix has to happen in whichever repo up the parent chain actually
  declares the version.
- That chain can be **more than one hop long** — Repo A depends on Repo B,
  which depends on Repo C, and so on.

Today, the system has no way to look past the single repo it was told to
scan. Its existing fallback rule is: "if the vulnerable component isn't
declared directly in this repo's `pom.xml`, treat it as unfixable — file a
GitHub Issue for a human instead of attempting a fix." That's a *safe*
fallback, but it throws away every case where the company actually owns the
upstream parent repo and the chain **could** be fixed automatically.

## 3. The core idea: resolve the chain, fix at the root, cascade the fix down

Instead of asking "can I fix this in the one repo I was pointed at?", the
process becomes: "where in the ownership chain does this version actually
live, and can I fix it there and propagate the fix downward?"

### Phase 1 — Walk the parent chain to find the true owner

When a flagged component's version isn't found in the target repo's own
POM:

1. Read the target repo's `<parent>` coordinates (group ID, artifact ID,
   version).
2. Resolve those coordinates to the repository that produces that parent
   artifact.
3. Fetch that repo's POM and check whether *it* declares the vulnerable
   component's version (directly, or via `dependencyManagement`).
4. If not found, repeat the same walk using *that* repo's own `<parent>`
   coordinates.
5. Stop when either:
   - a repo in the chain is found that actually declares the version
     (this is the **root-cause repo**), or
   - the chain leads outside the company's own repositories (e.g., a public
     third-party BOM/parent that the company doesn't control), or
   - a safety limit on chain depth is reached (to guard against circular or
     runaway parent references).

This produces an ordered list of repositories, from root cause down to the
originally-flagged leaf repo — e.g. `[Repo C, Repo B, Repo A]`.

### Phase 2 — Branch on ownership

- **Root-cause repo is external / third-party** → nothing the company can
  patch. This is the existing safe fallback: create a GitHub Issue, no fix
  attempted. No change needed here.
- **Root-cause repo is internal** (the company owns it, even if it's a
  shared platform/parent-pom repo maintained by a different team) → this is
  now a **fixable chain**, and the process below applies.

### Phase 3 — Fix the root, using the existing machinery unchanged

Run the normal Fixer → PR → CI → Watcher flow against the **root-cause
repo** exactly as it already runs today for any other repo: bump the
version, let the AI fix any resulting code breakage, verify the build and
tests, open a PR, wait for CI, retry on failure up to the existing bound.
No new "fixing" logic is required here — it's the same flow, just pointed at
a different repo than the one Nexus originally flagged.

### Phase 4 — Cascade the fix down the chain

Once the root-cause repo's fix is merged and a new version of that artifact
is published (to the internal artifact repository, e.g. Nexus/Artifactory),
every repo directly beneath it in the chain needs a much simpler PR: bump
its own `<parent>` version (or dependency version) to point at the newly
published artifact. This is a **version-only bump** — normally no source
code changes are needed, because the actual vulnerable code lived in the
parent, not the child. This simpler fix reuses the same PR/CI/Watcher
plumbing as any other Fixer run; it's just a lighter-weight case of it.

This repeats down the chain, one hop at a time, until the originally-flagged
leaf repo has been updated.

### Phase 5 — New coordination state (not new agents)

The only genuinely new piece is a way to express "this repo's fix is waiting
on another repo's fix to land first." Introduce a status such as
`BLOCKED_ON_UPSTREAM` on the tracking record for each downstream repo in the
chain, pointing at the upstream repo's own tracking record. A downstream
repo only becomes eligible to run its own (simple, version-bump) Fixer pass
once the upstream tracking record reaches "merged and published." No new
agent is required — this is an extra state and a dependency pointer layered
on top of the existing Fixer/Watcher/tracking-record system.

## 4. Concrete worked example

1. Nexus scans **Repo A** and flags `jackson-databind` as vulnerable.
2. Repo A's `pom.xml` has no `jackson-databind` version anywhere — it has a
   `<parent>` pointing at **Repo B** (`company-parent-pom`).
3. Repo B's POM doesn't declare it either — Repo B's `<parent>` points at
   **Repo C** (`org-base-parent-pom`).
4. Repo C's POM has `<jackson.version>2.9.9</jackson.version>` in its
   `dependencyManagement` — this is the root cause.
5. Repo C is owned by the company (a platform team's repo) → fixable chain.
6. The Fixer runs against **Repo C**: bumps `jackson.version`, verifies
   build/tests, opens a PR, CI passes, PR is merged, a new version of
   `org-base-parent-pom` is published.
7. **Repo B** is now unblocked: its Fixer run is a simple version bump of
   its `<parent>` version to the new `org-base-parent-pom` release, PR,
   CI, merge, publish.
8. **Repo A** is now unblocked: same simple version-bump pattern against its
   `<parent>` reference to the new `company-parent-pom` release. Once
   merged, the original vulnerability Nexus flagged is actually resolved.

## 5. Open questions / what's needed to make this work in practice

- **GAV-to-repo mapping**: given a Maven coordinate (`groupId:artifactId:version`),
  how do we know which GitHub repository produces it? Options: a
  company-maintained registry/mapping file, inferring it from the `<scm>`
  block inside the published POM, or querying the internal artifact
  repository's metadata API.
- **Chain depth limits**: how many parent hops should be walked before giving
  up, to protect against misconfigured or circular parent references?
- **Cross-team ownership**: the root-cause repo may be owned by a different
  team than the one that reported the vulnerability. Does the automated PR
  go to that team directly, or does it need a different notification path?
- **Release cadence**: how long should a downstream repo wait in
  `BLOCKED_ON_UPSTREAM` before a human is notified, if the upstream team is
  slow to merge/release?

## 6. Summary — key concepts for a mind map

- **Problem**: transitive dependency versions can be inherited from a parent
  POM that lives in a different repo, potentially several repos up a chain.
- **Chain walk**: resolve `<parent>` references repo-by-repo until the repo
  that actually declares the vulnerable version is found.
- **Ownership branch**: internal chain → fixable; external/third-party →
  existing Issue fallback, unchanged.
- **Root-cause fix**: run the existing Fixer/PR/CI/Watcher flow unchanged,
  just targeted at the repo that actually owns the version.
- **Cascade**: each downstream repo gets a lightweight version-only bump PR,
  repeated down the chain.
- **New state**: `BLOCKED_ON_UPSTREAM` tracking status links a downstream
  repo's fix to its upstream dependency, gating when it becomes eligible to
  run.
- **No new agents**: the idea reuses the existing Fixer and Watcher
  machinery twice over (once for the real fix, N times for cascading
  version bumps) rather than inventing new fixing logic.