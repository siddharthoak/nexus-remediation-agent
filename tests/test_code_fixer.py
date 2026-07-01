"""
TEST-02: Unit tests for code_fixer.py.

Tests version bump logic and the no-unrelated-refactoring guardrail.
All model API calls are mocked — no real Anthropic API calls are made.
"""

import json
import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "agents", "fixer"))

from code_fixer import CodeFixer, ChangeSummary, PomXMLError, CodeFixerError


# ── Fixtures ──────────────────────────────────────────────────────────────────

SAMPLE_POM_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>com.example</groupId>
  <artifactId>my-app</artifactId>
  <version>1.0.0</version>
  <dependencies>
    <dependency>
      <groupId>org.example</groupId>
      <artifactId>log4j</artifactId>
      <version>1.2.17</version>
    </dependency>
    <dependency>
      <groupId>com.google.guava</groupId>
      <artifactId>guava</artifactId>
      <version>30.1</version>
    </dependency>
  </dependencies>
</project>
"""

# Realistic canned model response: version bump + one code change
MOCK_MODEL_RESPONSE_WITH_CHANGES = {
    "rationale": (
        "log4j 2.x introduced a new logging API. The static Logger factory moved from "
        "org.apache.log4j.Logger.getLogger() to org.apache.logging.log4j.LogManager.getLogger()."
    ),
    "files_to_change": [
        {
            "relative_path": "src/main/java/com/example/App.java",
            "change_description": "Update Logger import and factory call for log4j 2.x API",
            "find": "import org.apache.log4j.Logger;",
            "replace": "import org.apache.logging.log4j.LogManager;\nimport org.apache.logging.log4j.Logger;",
        }
    ],
}

# Canned model response: no code changes needed (only pom.xml bump)
MOCK_MODEL_RESPONSE_NO_CHANGES = {
    "rationale": "This is a patch-level bump with no API changes. Only pom.xml version update required.",
    "files_to_change": [],
}


# ── Repo fixture setup ────────────────────────────────────────────────────────

@pytest.fixture
def repo_dir():
    """Create a minimal fake Java repo for testing. Cleaned up after each test."""
    tmpdir = tempfile.mkdtemp(prefix="test-repo-")
    # pom.xml
    (Path(tmpdir) / "pom.xml").write_text(SAMPLE_POM_XML, encoding="utf-8")
    # Java source files
    src = Path(tmpdir) / "src" / "main" / "java" / "com" / "example"
    src.mkdir(parents=True)
    (src / "App.java").write_text(
        "import org.apache.log4j.Logger;\npublic class App { }", encoding="utf-8"
    )
    (src / "Util.java").write_text(
        "public class Util { }", encoding="utf-8"
    )
    # An unrelated config file that must NOT be touched
    (Path(tmpdir) / "config.properties").write_text(
        "app.name=my-app\n", encoding="utf-8"
    )
    yield tmpdir
    shutil.rmtree(tmpdir, ignore_errors=True)


def _make_fixer(repo_dir: str, model_response: dict) -> CodeFixer:
    """Build a CodeFixer with the model call mocked to return `model_response`."""
    fixer = CodeFixer(repo_path=repo_dir, model_deployment_name="test-model")
    mock_msg = MagicMock()
    mock_msg.content = [MagicMock(text=f"```json\n{json.dumps(model_response)}\n```")]
    fixer._client = MagicMock()
    fixer._client.messages.create.return_value = mock_msg
    return fixer


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestPomVersionBump:
    """pom.xml version is correctly updated using an XML parser."""

    def test_version_is_bumped_correctly(self, repo_dir):
        fixer = _make_fixer(repo_dir, MOCK_MODEL_RESPONSE_NO_CHANGES)
        fixer.fix_vulnerability("org.example:log4j", "1.2.17", "2.20.0")

        pom = (Path(repo_dir) / "pom.xml").read_text()
        assert "<version>2.20.0</version>" in pom

    def test_old_version_is_removed(self, repo_dir):
        fixer = _make_fixer(repo_dir, MOCK_MODEL_RESPONSE_NO_CHANGES)
        fixer.fix_vulnerability("org.example:log4j", "1.2.17", "2.20.0")

        pom = (Path(repo_dir) / "pom.xml").read_text()
        # The old version should no longer appear as a dependency version
        # (guava is still 30.1 so we can't assert "1.2.17" is fully absent from the file)
        import xml.etree.ElementTree as ET
        tree = ET.parse(str(Path(repo_dir) / "pom.xml"))
        ns = {"m": "http://maven.apache.org/POM/4.0.0"}
        versions = [el.text for el in tree.getroot().findall(".//m:dependency/m:version", ns)]
        assert "1.2.17" not in versions
        assert "2.20.0" in versions

    def test_unrelated_dependency_version_is_unchanged(self, repo_dir):
        fixer = _make_fixer(repo_dir, MOCK_MODEL_RESPONSE_NO_CHANGES)
        fixer.fix_vulnerability("org.example:log4j", "1.2.17", "2.20.0")

        pom = (Path(repo_dir) / "pom.xml").read_text()
        assert "30.1" in pom  # guava version must be untouched

    def test_missing_dependency_raises_pom_error(self, repo_dir):
        fixer = _make_fixer(repo_dir, MOCK_MODEL_RESPONSE_NO_CHANGES)
        with pytest.raises(PomXMLError):
            fixer.fix_vulnerability("org.nonexistent:library", "1.0.0", "2.0.0")


class TestChangeSummary:
    """ChangeSummary is correctly populated from model output."""

    def test_change_summary_has_correct_component_and_versions(self, repo_dir):
        fixer = _make_fixer(repo_dir, MOCK_MODEL_RESPONSE_WITH_CHANGES)
        summary = fixer.fix_vulnerability(
            "org.example:log4j", "1.2.17", "2.20.0", cve_ids=["CVE-2021-44228"]
        )

        assert summary.component_name == "org.example:log4j"
        assert summary.old_version == "1.2.17"
        assert summary.new_version == "2.20.0"

    def test_change_summary_files_changed_includes_pom(self, repo_dir):
        fixer = _make_fixer(repo_dir, MOCK_MODEL_RESPONSE_WITH_CHANGES)
        summary = fixer.fix_vulnerability("org.example:log4j", "1.2.17", "2.20.0")

        assert "pom.xml" in summary.files_changed

    def test_change_summary_includes_modified_java_file(self, repo_dir):
        fixer = _make_fixer(repo_dir, MOCK_MODEL_RESPONSE_WITH_CHANGES)
        summary = fixer.fix_vulnerability("org.example:log4j", "1.2.17", "2.20.0")

        assert any("App.java" in f for f in summary.files_changed)

    def test_rationale_is_populated_from_model_response(self, repo_dir):
        fixer = _make_fixer(repo_dir, MOCK_MODEL_RESPONSE_WITH_CHANGES)
        summary = fixer.fix_vulnerability("org.example:log4j", "1.2.17", "2.20.0")

        assert len(summary.rationale) > 0
        assert "log4j" in summary.rationale.lower()


class TestNoUnrelatedRefactoringGuardrail:
    """
    GUARDRAIL TEST: Only files explicitly identified by the model may be modified.

    This test is protecting a hard safety property: the agent must not modify any file
    outside the set it explicitly identified as needing changes. Any other file must be
    byte-for-byte identical after the fixer runs.
    """

    def test_only_identified_files_are_modified(self, repo_dir):
        fixer = _make_fixer(repo_dir, MOCK_MODEL_RESPONSE_WITH_CHANGES)

        # Record the state of every file BEFORE the fixer runs
        before = {}
        for f in Path(repo_dir).rglob("*"):
            if f.is_file():
                before[str(f)] = f.read_bytes()

        fixer.fix_vulnerability("org.example:log4j", "1.2.17", "2.20.0")

        # Record state AFTER
        after = {}
        for f in Path(repo_dir).rglob("*"):
            if f.is_file():
                after[str(f)] = f.read_bytes()

        # The model identified App.java and pom.xml — only those two may differ
        allowed_to_change = {
            str(Path(repo_dir) / "pom.xml"),
            str(Path(repo_dir) / "src" / "main" / "java" / "com" / "example" / "App.java"),
        }

        for path, content in before.items():
            if path not in allowed_to_change:
                assert after.get(path) == content, (
                    f"GUARDRAIL VIOLATION: file '{path}' was modified but was NOT in the "
                    f"model's identified change set. The no-unrelated-refactoring guardrail is broken."
                )

    def test_util_java_is_not_touched(self, repo_dir):
        """Explicit check that Util.java (not identified) is unchanged."""
        util_path = Path(repo_dir) / "src" / "main" / "java" / "com" / "example" / "Util.java"
        before = util_path.read_bytes()

        fixer = _make_fixer(repo_dir, MOCK_MODEL_RESPONSE_WITH_CHANGES)
        fixer.fix_vulnerability("org.example:log4j", "1.2.17", "2.20.0")

        assert util_path.read_bytes() == before, (
            "Util.java was modified but the model did not identify it as needing changes."
        )

    def test_config_properties_is_not_touched(self, repo_dir):
        """Explicit check that config.properties is unchanged."""
        config_path = Path(repo_dir) / "config.properties"
        before = config_path.read_bytes()

        fixer = _make_fixer(repo_dir, MOCK_MODEL_RESPONSE_WITH_CHANGES)
        fixer.fix_vulnerability("org.example:log4j", "1.2.17", "2.20.0")

        assert config_path.read_bytes() == before
