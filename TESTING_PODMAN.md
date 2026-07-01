# Local Testing Guide — Fixer Multi-Framework Support (Podman)

This walks through validating the Fixer's build framework abstraction
(`agents/fixer/frameworks/` — see PLAN.md section 4.7) on a local machine using
Podman instead of Docker. Commands are 1:1 with Docker's CLI; swap `podman` for
`docker` if you'd rather use Docker Desktop.

Everything in Steps 1–4 runs with **no external credentials** (no Anthropic key, no
GitHub PAT, no Nexus IQ key, no Azure/Cosmos). Step 5 is optional and requires real
credentials for a true end-to-end run.

---

## 0. Prerequisites

Install Podman and start its VM (macOS/Windows only — Podman needs a Linux VM to run
containers, unlike Docker Desktop which bundles one):

```bash
brew install podman
podman machine init
podman machine start
podman info   # sanity check — should print without errors
```

On Linux, `podman` runs natively — skip the `podman machine` steps.

---

## 1. Build the Fixer image

From the repo root:

```bash
podman build -f agents/fixer/Dockerfile -t fixer-agent-local .
```

This installs all four supported runtimes into one image (JDK 17 + Maven, Node 20 +
npm, Python 3.11) and runs a build-time smoke test as the last `RUN` step — if any
runtime is missing, `podman build` itself fails here rather than failing later at
container runtime. A clean build ending in `COMMIT` / `Successfully tagged` confirms
this passed.

---

## 2. Runtime smoke test

Confirm all four runtimes are actually present and the expected versions:

```bash
podman run --rm fixer-agent-local bash -c '
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

podman run --rm -v /tmp/fixer-smoke:/smoke:Z -w /app fixer-agent-local python3 -c '
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

Expected: `detected framework: maven`, `manifest bumped: pom.xml`, `build success:
True` (the trivial `App.java` compiles cleanly with `mvn compile` once `commons-lang3`
resolves from Maven Central), and `test status: NO_TESTS_FOUND` (no `src/test/java`
directory in this fixture). This was verified end-to-end while writing this guide.

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
podman run --rm -v "$(pwd)":/repo:Z -w /repo fixer-agent-local bash -c '
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

This actually calls Nexus IQ, clones from GitHub, calls the Anthropic API, and opens a
real PR — only do this against a disposable test repo. Requires real credentials, none
of which are read from `config.yaml` at container runtime (that file is only used by
`scripts/update_agent.py` to *deploy* the agent) — `main.py` reads these directly from
the environment:

```bash
podman run --rm \
  -e NEXUS_IQ_APP_PUBLIC_ID=<your-nexus-app-id> \
  -e GITHUB_REPO_TARGET=<org>/<repo> \
  -e GITHUB_PAT=<your-github-pat> \
  -e MODEL_DEPLOYMENT_NAME=claude-sonnet-5 \
  -e ANTHROPIC_API_KEY=<your-anthropic-key> \
  fixer-agent-local
```

With no `COSMOS_ENDPOINT` set, `make_tracking_store()` / `make_knowledge_store()` fall
back to `InMemoryTrackingStore` / `InMemoryKBStore` automatically — state just won't
persist past the container's lifetime, which is fine for a one-off local smoke test.

---

## Cleanup

```bash
podman rmi fixer-agent-local
rm -rf /tmp/fixer-smoke
```
