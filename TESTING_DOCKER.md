# Local Testing Guide — Fixer Multi-Framework Support (Docker)

This walks through validating the Fixer's build framework abstraction
(`agents/fixer/frameworks/` — see PLAN.md section 4.7) on a local machine using
Docker. Commands are 1:1 with Podman's CLI (see TESTING_PODMAN.md); swap `docker`
for `podman` if you'd rather use Podman.

Everything in Steps 1–4 runs with **no external credentials** (no Anthropic key, no
GitHub PAT, no Nexus IQ key, no Azure/Cosmos). Step 5 is optional and requires real
credentials for a true end-to-end run.

---

## 0. Prerequisites

Install Docker Desktop (macOS/Windows) or the Docker Engine (Linux):

```bash
# macOS/Windows: install Docker Desktop from
#   https://www.docker.com/products/docker-desktop
# Linux:
apt install docker.io        # Debian/Ubuntu
dnf install docker-ce         # Fedora/RHEL (or Docker's official repo)
```

Docker Desktop bundles its own Linux VM on macOS/Windows, so unlike Podman there's
no separate "machine init/start" step — start Docker Desktop and it's ready to use.

```bash
docker info   # sanity check — should print without errors
```

On Linux, either run as `sudo` or add your user to the `docker` group so `docker`
works without `sudo` — see
`https://docs.docker.com/engine/install/linux-postinstall/` if `docker info` fails
with a permission error.

---

## 1. Build the Fixer image

From the repo root:

```bash
docker build -f agents/fixer/Dockerfile -t fixer-agent-local .
```

This installs all four supported runtimes into one image (JDK 17 + Maven, Node 20 +
npm, Python 3.11) and runs a build-time smoke test as the last `RUN` step — if any
runtime is missing, `docker build` itself fails here rather than failing later at
container runtime. A clean build ending in `naming to docker.io/library/fixer-agent-local`
/ `Successfully tagged` confirms this passed.

The same image also bundles `agents/knowledge_agent/` and `agents/classifier/` —
those two have no `Dockerfile` of their own; `agents/fixer/main.py` imports
`KnowledgeAgent`/`Classifier` directly and runs them in-process, so their source
just needs to be on disk inside the Fixer's container (see section 6 for why this
matters for what you can and can't observe locally).

---

## 2. Runtime smoke test

Confirm all four runtimes are actually present and the expected versions:

```bash
docker run --rm fixer-agent-local bash -c '
  java -version
  echo ---
  mvn --version | head -3
  echo ---
  node --version
  echo ---
  npm --version
  echo ---
  python3 --version
  echo ---
  python3 -c "import tomllib; print(\"tomllib OK\")"
'
```

Expected output: OpenJDK 17.x, Maven 3.8.x, Node 20.x, npm 10.x, Python 3.11.x,
`tomllib OK`. If `mvn --version` reports a JDK version other than 17, the base image
tag has drifted — see the note in PLAN.md section 4.7 ("Docker strategy") about why
`python:3.11-slim-bookworm` is pinned explicitly rather than using the rolling
`python:3.11-slim` tag.

---

## 3. Framework detection + build/test dispatch, against fixture repos

This exercises `detect_framework()`, `bump_dependency()`, `build()`, and `test_unit()`
directly — the same code path the Fixer's `run_build`/`run_unit_tests` tools call —
without needing the Anthropic API. Mount a scratch directory into the container and
drive it with a short Python script.

```bash
mkdir -p /tmp/fixer-smoke/maven-repo/src/main/java/com/example
cat > /tmp/fixer-smoke/maven-repo/pom.xml <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>com.example</groupId>
  <artifactId>demo</artifactId>
  <version>1.0.0</version>
  <properties>
    <maven.compiler.source>17</maven.compiler.source>
    <maven.compiler.target>17</maven.compiler.target>
  </properties>
  <dependencies>
    <!-- A real, resolvable Maven Central artifact — needed so `mvn compile` can
         actually resolve dependencies over the network in this smoke test. -->
    <dependency>
      <groupId>org.apache.commons</groupId>
      <artifactId>commons-lang3</artifactId>
      <version>3.12.0</version>
    </dependency>
  </dependencies>
</project>
EOF
cat > /tmp/fixer-smoke/maven-repo/src/main/java/com/example/App.java <<'EOF'
package com.example;
public class App {
    public static void main(String[] args) {
        System.out.println("hello");
    }
}
EOF

docker run --rm -v /tmp/fixer-smoke:/smoke -w /app fixer-agent-local python3 -c '
import sys
sys.path.insert(0, ".")
from pathlib import Path
from frameworks import detect_framework

repo = Path("/smoke/maven-repo")
fw = detect_framework(repo)
print("detected framework:", fw.name if fw else None)

fw.bump_dependency(repo, "org.apache.commons:commons-lang3", "3.12.0", "3.14.0")
print("manifest bumped:", fw.manifest_file)
assert "3.14.0" in (repo / "pom.xml").read_text()

result = fw.build(repo)
print("build success:", result.success)
print(result.output[:2000])

test_result = fw.test_unit(repo)
print("test status:", test_result.status)  # expect NO_TESTS_FOUND — no src/test/java here
'
```

Note: the `:Z` SELinux relabel suffix used in TESTING_PODMAN.md's volume mounts is
omitted here — it's specific to rootless Podman on SELinux-enabled Linux hosts
(Fedora/RHEL). Docker doesn't need it; on a Linux host with SELinux enforcing and
Docker configured to honor SELinux labels, add `:z` (lowercase, shared) if you hit a
permission-denied error reading `/smoke` inside the container.

Expected: `detected framework: maven`, `manifest bumped: pom.xml`, `build success:
True` (the trivial `App.java` compiles cleanly with `mvn compile` once `commons-lang3`
resolves from Maven Central), and `test status: NO_TESTS_FOUND` (no `src/test/java`
directory in this fixture). This was verified end-to-end while writing the Podman
version of this guide (TESTING_PODMAN.md); the commands are otherwise identical.

Repeat with a `package.json` (`{"name":"demo","dependencies":{"lodash":"^4.17.15"}}`)
or a `requirements.txt` (`flask==2.0.0`) dropped into a fresh subdirectory of
`/tmp/fixer-smoke` to exercise the npm and Python paths the same way. There's no
Gradle CLI in the image by design — Gradle repos rely on their own `./gradlew`
wrapper, so a Gradle fixture needs a real `gradlew` script checked in to test
`build()`/`test_unit()` end-to-end; `bump_dependency()` on `build.gradle` can still be
exercised without one.

---

## 4. Run the automated test suite

The unit tests for the abstraction (`tests/test_frameworks.py`) and the Fixer's
tool-use loop (`tests/test_code_fixer.py`) mock `subprocess.run`, so they don't
actually need Maven/Node/etc. installed — they can run directly inside the container
against the mounted repo, or on the host if it has Python 3.11+ and the dependencies
installed.

**Inside the container (recommended — matches the deployed environment exactly):**

```bash
docker run --rm -v "$(pwd)":/repo -w /repo fixer-agent-local bash -c '
  pip install --quiet pytest
  pytest tests/test_frameworks.py tests/test_code_fixer.py -v
'
```

**On the host**, if you have Python 3.11+ available:

```bash
python3 -m pip install -r requirements.txt pytest
python3 -m pytest tests/test_frameworks.py tests/test_code_fixer.py -v
```

Both should report all tests passing.

---

## 5. (Optional, advanced) Full end-to-end fresh-scan run

This actually clones from GitHub, calls the Anthropic API, and opens a real PR — only do
this against a disposable test repo. `main.py` reads all of the following directly from
the environment (none of it comes from `config.yaml`, which is only used by
`scripts/update_agent.py` to *deploy* the agent).

### 5a. No live Nexus IQ Server? Use `DEPLOYMENT_MODE=local` + pre-generated scan reports

`main.py` calls `make_vulnerability_source()` (`agents/fixer/nexus_client.py`), which reads
`DEPLOYMENT_MODE` the same way `make_tracking_store()`/`make_knowledge_store()` already do:
`DEPLOYMENT_MODE=azure` → `NexusIQClient` (real Nexus IQ Server); `DEPLOYMENT_MODE=local` →
`ScanReportClient`, which parses Trivy/Grype/OWASP Dependency-Check JSON reports from
`SCAN_REPORT_PATH` instead. This is the only way to run the Fixer against a real repo
without Nexus IQ access at all.

If you already have Trivy/Grype reports generated for your target repo elsewhere (e.g. from
a prior run of a different agent, or a GitHub Actions scan artifact), point `SCAN_REPORT_PATH`
at that directory — no need to regenerate them:

```bash
docker run --rm \
  -v /path/to/existing/scan-reports:/reports \
  -e DEPLOYMENT_MODE=local \
  -e SCAN_REPORT_PATH=/reports \
  -e GITHUB_REPO_TARGET=<org>/<repo> \
  -e GITHUB_PAT=<your-github-pat> \
  -e MODEL_DEPLOYMENT_NAME=claude-sonnet-5 \
  -e ANTHROPIC_API_KEY=<your-anthropic-key> \
  fixer-agent-local
```

`SCAN_REPORT_PATH` should contain one or more of `trivy-report.json`, `grype-report.json`, or
`dependency-check-report/dependency-check-report.json`. `NEXUS_IQ_APP_PUBLIC_ID` isn't needed
in this mode — `ScanReportClient` ignores it.

### 5b. Have a real Nexus IQ Server? Use `DEPLOYMENT_MODE=azure`

```bash
docker run --rm \
  -e DEPLOYMENT_MODE=azure \
  -e NEXUS_IQ_ENDPOINT=<your-nexus-iq-base-url> \
  -e NEXUS_IQ_API_KEY=<your-nexus-iq-key> \
  -e NEXUS_IQ_APP_PUBLIC_ID=<your-nexus-app-id> \
  -e GITHUB_REPO_TARGET=<org>/<repo> \
  -e GITHUB_PAT=<your-github-pat> \
  -e MODEL_DEPLOYMENT_NAME=claude-sonnet-5 \
  -e ANTHROPIC_API_KEY=<your-anthropic-key> \
  fixer-agent-local
```

Note: `nexus_client.py`'s endpoint paths and response field names are marked `# FIXME` —
they're based on Sonatype's public docs, not yet validated against a real instance (see
PLAN.md section 6). Expect to need to adjust `_parse_policy_report()` once you see a real
response shape.

### Either way

With no `COSMOS_ENDPOINT` set, `make_tracking_store()` / `make_knowledge_store()` fall
back to `InMemoryTrackingStore` / `InMemoryKBStore` automatically — state just won't
persist past the container's lifetime, which is fine for a one-off local smoke test.

---

## 6. Full end-to-end: Watcher + Fixer + Knowledge Agent + Classifier together

Sections 1–5 exercise the Fixer in isolation, which already includes the Knowledge
Agent and Classifier — they're plain Python modules imported and run in-process
inside the Fixer (`agents/fixer/main.py`), not separate containers or services.
This section adds the **Watcher** into the loop, which is the part that actually
requires two separate container runs to coordinate with each other.

Uses `docker-compose.e2e.yml` (repo root) — two one-shot services, `fixer` and
`watcher`, both built from this repo and sharing one `.env` file. Neither is a
long-running server, so use `run --rm`, not `up`.

### 6.0 Why this needs a real Cosmos DB account (not the Emulator)

The Fixer and Watcher are separate container invocations — each run is its own
process. `InMemoryTrackingStore`/`InMemoryKBStore` (`agents/common/`) only live for
the lifetime of one process, so if the Fixer opens a PR using the in-memory backend,
a Watcher run in a *different* container will never see that tracking record — its
logs will just say `no tracking record found (opened outside the agent?)` and skip
every PR.

For the Watcher to see what the Fixer did, both need to point at the same
**persistent** backend — which today is only `CosmosTrackingStore`/`CosmosKBStore`.
The Cosmos DB Emulator won't help here: both classes hardcode
`DefaultAzureCredential()` (Azure AD auth) with no key-based fallback, and the
Emulator authenticates with a fixed master key, not Azure AD. So this section uses a
real (a disposable dev/test one is fine) Azure Cosmos DB account — the exact same
code path production uses, just reached from your laptop instead of a Hosted Agent.

**If you don't have a Cosmos account handy**, skip this section — section 5 already
gives you a genuine (if single-container) exercise of the Fixer's full pipeline
(Knowledge Agent, Classifier, CodeFixer, PR creation). The Watcher's own decision
logic (retry-bound checks, escalation, pattern learning) is separately covered by
`pytest tests/test_retry_controller.py tests/test_code_fixer.py -v`, which calls
`RetryGate`/`CodeFixer.run_retry_fix()` directly with fakes — no live container
hand-off required. That test suite is, today, the only way to see a *successful*
automatic retry exercised — see 6.3–6.4 for why.

### 6.1 Set up

1. Create (or reuse) a dev/test Azure Cosmos DB account (NoSQL API) and grant
   whichever identity the containers will authenticate as (your own `az login`
   identity, or a service principal — see step 3) the **Cosmos DB Built-in Data
   Contributor** data-plane role (PLAN.md section 5, step 3). The database/containers
   (`oss-remediation`/`tracking-records`/`kb-entries` by default) are created
   automatically on first write as long as the identity has that role.

2. Fill in `.env` (copy from `.env.example`):
   - Leave `DEPLOYMENT_MODE` **blank** (not `local`) — see the comment in
     `.env.example` for why.
   - Set `COSMOS_ENDPOINT` to your account's endpoint.
   - Set `FIXER_AGENT_ID` to any placeholder string (e.g. `local-placeholder`) —
     required so the Watcher doesn't crash at startup; see 6.3.

3. Make `DefaultAzureCredential` resolve *inside* the container. Two options:
   - **Quick / local dev**: run `az login` on your host, then mount your Azure CLI
     credentials into the container (added to the run commands below).
   - **Cleaner / CI-friendly**: create a service principal scoped to the Cosmos
     account and add these to `.env` instead — `DefaultAzureCredential` picks them
     up automatically, no volume mount needed:
     ```
     AZURE_CLIENT_ID=...
     AZURE_CLIENT_SECRET=...
     AZURE_TENANT_ID=...
     ```

### 6.2 Run the Fixer — opens a PR, state now lands in Cosmos

```bash
docker compose -f docker-compose.e2e.yml build fixer watcher

docker compose -f docker-compose.e2e.yml run --rm \
  -v ~/.azure:/root/.azure:ro \
  fixer
```
(drop the `-v ~/.azure:/root/.azure:ro` line if you used the service-principal
option in 6.1 instead)

This is the same fresh-scan flow as section 5, except `make_tracking_store()` /
`make_knowledge_store()` now resolve to the Cosmos-backed classes (since
`COSMOS_ENDPOINT` is set and `DEPLOYMENT_MODE` is blank) — confirm this in the logs:
you should NOT see `DEPLOYMENT_MODE=local — using InMemoryTrackingStore`.

Then, in your test repo on GitHub, **deliberately break CI** on the PR the Fixer
just opened (only ever do this against a disposable test repo — same caveat as
section 5).

### 6.3 Run the Watcher — sees the PR, detects the CI failure

```bash
docker compose -f docker-compose.e2e.yml run --rm \
  -v ~/.azure:/root/.azure:ro \
  watcher
```

Expected log sequence:
1. `Watching 1 open remediation PR(s).` — it found the PR via the `fix/` branch
   prefix and, because Cosmos is shared, it also finds the Fixer's tracking record
   (this is the part that would silently fail with `no tracking record found` if you
   were still on `DEPLOYMENT_MODE=local`).
2. `PR #N: CI failed. Delegating to RetryGate.`
3. `PR #N: created RETRY_REQUESTED record ... (attempt 1/3)` — `RetryGate` wrote the
   retry record and is about to invoke the Fixer.
4. `PR #N: Failed to invoke Fixer: ... Marking as ESCALATED.` — **expected locally.**

Step 4 happens because `make_fixer_invoker()` (`agents/watcher/retry_gate.py`)
defaults to `AafFixerInvoker`, which calls the Azure AI Foundry SDK
(`azure.ai.projects`) to trigger a new Hosted Agent run — that package isn't even
installed in the Watcher's image (`agents/watcher/requirements.txt` has
`azure-cosmos`/`azure-identity` but not `azure-ai-projects`), and there's no live
Foundry project to call from a laptop anyway. The alternative `HttpFixerInvoker`
POSTs to a `FIXER_RETRY_URL`, but the Fixer doesn't run as an HTTP server anywhere
in this codebase (`agents/fixer/main.py` is a one-shot script, not a `uvicorn`
app) — so there is currently no way to automatically complete the Watcher→Fixer
retry hand-off outside of a real Azure AI Foundry deployment.

**This is a known gap, not a mistake in your setup.** What you've just verified for
real: PR discovery, tracking-record lookup, CI-failure detection, the retry-bound
check, `RETRY_REQUESTED` record creation, and safe escalation-on-invoke-failure — all
of the Watcher's logic except the final hand-off. For the hand-off itself, and for a
*successful* multi-attempt retry (not just the first escalation), see 6.4.

### 6.4 What the container-based test above can't show you — and where it's tested instead

```bash
pip install -r requirements.txt
pytest tests/test_retry_controller.py tests/test_code_fixer.py -v
```

These tests call `RetryGate.process_ci_failure()` and `CodeFixer.run_retry_fix()`
directly with mocked collaborators, so they exercise the full state machine —
including a CI failure that *does* get a successful retry, and a retry that
exhausts `MAX_RETRY_ATTEMPTS` and escalates — without needing one live process to
invoke another live process. Treat this suite as the source of truth for retry
logic; treat 6.1–6.3 as the source of truth for "does this actually build and run
as containers, with real Cosmos and GitHub, end to end."

### 6.5 Inspect what happened

```bash
pip install streamlit pandas
export DEPLOYMENT_MODE=azure
export COSMOS_ENDPOINT=<same endpoint as .env>
az login
streamlit run streamlit_dashboard.py
```

The **Run History** tab shows the tracking records you just created (including the
`ESCALATED` one), **Retry Lineage** shows the parent→child chain via
`parent_tracking_id`, and **Knowledge Base** shows whatever the Knowledge Agent
hydrated during the Fixer's fresh scan.

---

## Cleanup

```bash
docker rmi fixer-agent-local
rm -rf /tmp/fixer-smoke
```

If you also ran section 6:

```bash
docker compose -f docker-compose.e2e.yml down --rmi local
```
