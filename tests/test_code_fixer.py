"""
TEST-02: Unit tests for code_fixer.py.

Tests the tool-use loop (mocked Anthropic client — no real API calls), the
no-unrelated-refactoring guardrail, and framework detection/reporting on ChangeSummary.
"""

import json
import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "agents"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "agents", "fixer"))

from code_fixer import CodeFixer, CodeFixerError
from frameworks.maven import PomXMLError
from common.knowledge_store import KnowledgeEntry


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

# Realistic canned end_turn response: one code change via apply_file_change, then JSON.
RATIONALE_WITH_CHANGES = (
    "log4j 2.x introduced a new logging API. The static Logger factory moved from "
    "org.apache.log4j.Logger.getLogger() to org.apache.logging.log4j.LogManager.getLogger()."
)
RATIONALE_NO_CHANGES = "This is a patch-level bump with no API changes."


@pytest.fixture
def repo_dir():
    """Create a minimal fake Java/Maven repo for testing. Cleaned up after each test."""
    tmpdir = tempfile.mkdtemp(prefix="test-repo-")
    (Path(tmpdir) / "pom.xml").write_text(SAMPLE_POM_XML, encoding="utf-8")
    src = Path(tmpdir) / "src" / "main" / "java" / "com" / "example"
    src.mkdir(parents=True)
    (src / "App.java").write_text(
        "import org.apache.log4j.Logger;\npublic class App { }", encoding="utf-8"
    )
    (src / "Util.java").write_text("public class Util { }", encoding="utf-8")
    (Path(tmpdir) / "config.properties").write_text("app.name=my-app\n", encoding="utf-8")
    yield tmpdir
    shutil.rmtree(tmpdir, ignore_errors=True)


def _end_turn_response(rationale: str):
    msg = MagicMock()
    msg.stop_reason = "end_turn"
    msg.content = [MagicMock(text=f"```json\n{json.dumps({'rationale': rationale})}\n```")]
    msg.usage = MagicMock(input_tokens=100, output_tokens=50)
    return msg


def _tool_use_response(tool_name: str, tool_input: dict, tool_use_id: str = "tool_1"):
    block = MagicMock()
    block.type = "tool_use"
    block.name = tool_name
    block.input = tool_input
    block.id = tool_use_id

    msg = MagicMock()
    msg.stop_reason = "tool_use"
    msg.content = [block]
    msg.usage = MagicMock(input_tokens=100, output_tokens=50)
    return msg


def _make_fixer(repo_dir: str, responses: list, kb_store=None) -> CodeFixer:
    """Build a CodeFixer whose model call returns each of `responses` in sequence."""
    fixer = CodeFixer(repo_path=repo_dir, model_deployment_name="test-model", kb_store=kb_store)
    fixer._client = MagicMock()
    fixer._client.messages.create.side_effect = responses
    return fixer


def _kb_entry(patterns: list, source="tier1_learned", confidence="high") -> KnowledgeEntry:
    return KnowledgeEntry(
        entry_id="test-entry",
        component_name="org.example:log4j",
        from_version="1.2.17",
        to_version="2.20.0",
        from_major=1,
        to_major=2,
        source=source,
        patterns=patterns,
        confidence=confidence,
    )


def _run(fixer, current_version="1.2.17", target_version="2.20.0"):
    return fixer._execute_fix(
        component_name="org.example:log4j",
        current_version=current_version,
        target_version=target_version,
        cve_ids=["CVE-2021-44228"],
        failure_log_excerpt=None,
    )


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestFrameworkDetectionAtInit:
    def test_maven_repo_is_detected(self, repo_dir):
        fixer = _make_fixer(repo_dir, [_end_turn_response(RATIONALE_NO_CHANGES)])
        assert fixer._framework is not None
        assert fixer._framework.name == "maven"

    def test_repo_with_no_manifest_degrades_to_none(self, tmp_path):
        fixer = _make_fixer(str(tmp_path), [_end_turn_response(RATIONALE_NO_CHANGES)])
        assert fixer._framework is None


class TestPomVersionBump:
    """pom.xml version is correctly updated via the Maven framework's XML parser."""

    def test_version_is_bumped_correctly(self, repo_dir):
        fixer = _make_fixer(repo_dir, [_end_turn_response(RATIONALE_NO_CHANGES)])
        _run(fixer)

        pom = (Path(repo_dir) / "pom.xml").read_text()
        assert "<version>2.20.0</version>" in pom

    def test_unrelated_dependency_version_is_unchanged(self, repo_dir):
        fixer = _make_fixer(repo_dir, [_end_turn_response(RATIONALE_NO_CHANGES)])
        _run(fixer)

        pom = (Path(repo_dir) / "pom.xml").read_text()
        assert "30.1" in pom  # guava version must be untouched

    def test_missing_dependency_raises_pom_error(self, repo_dir):
        fixer = _make_fixer(repo_dir, [_end_turn_response(RATIONALE_NO_CHANGES)])
        with pytest.raises(PomXMLError):
            fixer._execute_fix(
                component_name="org.nonexistent:library",
                current_version="1.0.0",
                target_version="2.0.0",
                cve_ids=[],
                failure_log_excerpt=None,
            )


class TestChangeSummary:
    """ChangeSummary is correctly populated from model output plus framework metadata."""

    def test_change_summary_has_correct_component_and_versions(self, repo_dir):
        fixer = _make_fixer(repo_dir, [_end_turn_response(RATIONALE_WITH_CHANGES)])
        summary = _run(fixer)

        assert summary.component_name == "org.example:log4j"
        assert summary.old_version == "1.2.17"
        assert summary.new_version == "2.20.0"

    def test_change_summary_files_changed_includes_manifest(self, repo_dir):
        fixer = _make_fixer(repo_dir, [_end_turn_response(RATIONALE_NO_CHANGES)])
        summary = _run(fixer)

        assert "pom.xml" in summary.files_changed

    def test_change_summary_includes_applied_file_change(self, repo_dir):
        responses = [
            _tool_use_response(
                "apply_file_change",
                {
                    "relative_path": "src/main/java/com/example/App.java",
                    "find": "import org.apache.log4j.Logger;",
                    "replace": "import org.apache.logging.log4j.LogManager;",
                },
            ),
            _end_turn_response(RATIONALE_WITH_CHANGES),
        ]
        fixer = _make_fixer(repo_dir, responses)
        summary = _run(fixer)

        assert any("App.java" in f for f in summary.files_changed)
        content = (Path(repo_dir) / "src/main/java/com/example/App.java").read_text()
        assert "LogManager" in content

    def test_rationale_is_populated_from_model_response(self, repo_dir):
        fixer = _make_fixer(repo_dir, [_end_turn_response(RATIONALE_WITH_CHANGES)])
        summary = _run(fixer)

        assert len(summary.rationale) > 0
        assert "log4j" in summary.rationale.lower()

    def test_framework_detected_is_recorded(self, repo_dir):
        fixer = _make_fixer(repo_dir, [_end_turn_response(RATIONALE_NO_CHANGES)])
        summary = _run(fixer)

        assert summary.framework_detected == "maven"

    def test_unit_test_status_defaults_to_none_when_never_called(self, repo_dir):
        fixer = _make_fixer(repo_dir, [_end_turn_response(RATIONALE_NO_CHANGES)])
        summary = _run(fixer)

        assert summary.unit_test_status is None


class TestUnitTestStatusReporting:
    """run_unit_tests() outcomes correctly map onto ChangeSummary.unit_test_status."""

    def test_no_tests_found_is_recorded(self, repo_dir):
        responses = [
            _tool_use_response("run_unit_tests", {}),
            _end_turn_response(RATIONALE_NO_CHANGES),
        ]
        fixer = _make_fixer(repo_dir, responses)
        summary = _run(fixer)

        # repo_dir fixture has no src/test/java — Maven's test_unit() returns NO_TESTS_FOUND
        assert summary.unit_test_status == "NO_TESTS_FOUND"

    def test_still_failing_tests_map_to_soft_fail(self, repo_dir):
        # Give the repo an actual unit test file so Maven's test_unit() doesn't
        # short-circuit to NO_TESTS_FOUND before even invoking `mvn test`.
        test_dir = Path(repo_dir) / "src" / "test" / "java"
        test_dir.mkdir(parents=True)
        (test_dir / "AppTest.java").write_text("class AppTest {}", encoding="utf-8")

        responses = [
            _tool_use_response("run_unit_tests", {}),
            _end_turn_response(RATIONALE_NO_CHANGES),
        ]
        fixer = _make_fixer(repo_dir, responses)
        with patch(
            "frameworks.maven.subprocess.run",
            return_value=MagicMock(returncode=1, stdout="", stderr="1 test failed"),
        ):
            summary = _run(fixer)

        # Soft gate: PR is still opened (no exception raised), failure is recorded for the
        # PR description / tracking record instead of blocking.
        assert summary.unit_test_status == "SOFT_FAIL"


class TestNoUnrelatedRefactoringGuardrail:
    """
    GUARDRAIL TEST: Only files the model explicitly touches via apply_file_change may change.
    """

    def test_only_identified_files_are_modified(self, repo_dir):
        responses = [
            _tool_use_response(
                "apply_file_change",
                {
                    "relative_path": "src/main/java/com/example/App.java",
                    "find": "import org.apache.log4j.Logger;",
                    "replace": (
                        "import org.apache.logging.log4j.LogManager;\n"
                        "import org.apache.logging.log4j.Logger;"
                    ),
                },
            ),
            _end_turn_response(RATIONALE_WITH_CHANGES),
        ]
        fixer = _make_fixer(repo_dir, responses)

        before = {
            str(f): f.read_bytes() for f in Path(repo_dir).rglob("*") if f.is_file()
        }

        _run(fixer)

        after = {
            str(f): f.read_bytes() for f in Path(repo_dir).rglob("*") if f.is_file()
        }

        allowed_to_change = {
            str(Path(repo_dir) / "pom.xml"),
            str(Path(repo_dir) / "src" / "main" / "java" / "com" / "example" / "App.java"),
        }

        for path, content in before.items():
            if path not in allowed_to_change:
                assert after.get(path) == content, (
                    f"GUARDRAIL VIOLATION: file '{path}' was modified but was NOT in the "
                    f"model's identified change set."
                )

    def test_util_java_is_not_touched(self, repo_dir):
        util_path = Path(repo_dir) / "src" / "main" / "java" / "com" / "example" / "Util.java"
        before = util_path.read_bytes()

        fixer = _make_fixer(repo_dir, [_end_turn_response(RATIONALE_NO_CHANGES)])
        _run(fixer)

        assert util_path.read_bytes() == before

    def test_config_properties_is_not_touched(self, repo_dir):
        config_path = Path(repo_dir) / "config.properties"
        before = config_path.read_bytes()

        fixer = _make_fixer(repo_dir, [_end_turn_response(RATIONALE_NO_CHANGES)])
        _run(fixer)

        assert config_path.read_bytes() == before


class TestApplyFileChangeErrorSurfacesToClaude:
    """apply_file_change never silently no-ops — a bad find string returns an ERROR
    tool_result in the same turn instead of a swallowed failure."""

    def test_bad_find_string_returns_error_and_model_can_retry(self, repo_dir):
        responses = [
            _tool_use_response(
                "apply_file_change",
                {
                    "relative_path": "src/main/java/com/example/App.java",
                    "find": "this string does not exist in the file",
                    "replace": "replacement",
                },
            ),
            _end_turn_response(RATIONALE_NO_CHANGES),
        ]
        fixer = _make_fixer(repo_dir, responses)
        summary = _run(fixer)

        # The file must be untouched — the bad find string was never applied.
        content = (Path(repo_dir) / "src/main/java/com/example/App.java").read_text()
        assert "import org.apache.log4j.Logger;" in content
        assert "App.java" not in "".join(summary.files_changed)

    def test_model_did_not_produce_final_answer_raises(self, repo_dir):
        # MAX_TOOL_ROUNDS repeated tool_use responses, never reaching end_turn.
        from code_fixer import MAX_TOOL_ROUNDS
        responses = [_tool_use_response("read_file", {"relative_path": "pom.xml"})] * MAX_TOOL_ROUNDS
        fixer = _make_fixer(repo_dir, responses)
        with pytest.raises(CodeFixerError):
            _run(fixer)


class TestKBHitFastPath:
    """KB-hit fast path (Tier 1/2/knowledge_agent patterns applied with no model call)."""

    def test_matching_pattern_applied_with_no_model_call(self, repo_dir):
        kb_store = MagicMock()
        kb_store.find_applicable.return_value = _kb_entry([
            {
                "find": "import org.apache.log4j.Logger;",
                "replace": "import org.apache.logging.log4j.LogManager;",
                "description": "log4j 1.x to 2.x Logger import change",
            }
        ])
        # No responses queued — a model call would raise StopIteration and fail the test.
        fixer = _make_fixer(repo_dir, [], kb_store=kb_store)

        # Mock the actual build/test gates — org.example:log4j is a fictional artifact
        # that a real `mvn compile` could never resolve; only the pattern-application
        # and gate-sequencing logic is under test here.
        with patch.object(fixer._framework, "build", return_value=MagicMock(success=True)), \
             patch.object(fixer._framework, "test_unit", return_value=MagicMock(status="NO_TESTS_FOUND")):
            summary = _run(fixer)

        assert summary.kb_hit is True
        assert summary.prompt_tokens == 0
        assert summary.completion_tokens == 0
        content = (Path(repo_dir) / "src/main/java/com/example/App.java").read_text()
        assert "LogManager" in content
        assert any("App.java" in f for f in summary.files_changed)

    def test_no_matching_pattern_falls_back_to_model(self, repo_dir):
        kb_store = MagicMock()
        kb_store.find_applicable.return_value = _kb_entry([
            {"find": "this string is not in any file", "replace": "x", "description": "n/a"}
        ])
        fixer = _make_fixer(repo_dir, [_end_turn_response(RATIONALE_NO_CHANGES)], kb_store=kb_store)

        summary = _run(fixer)

        assert summary.kb_hit is False
        assert summary.prompt_tokens > 0

    def test_pattern_matches_but_build_fails_falls_back_to_model_with_kb_context(self, repo_dir):
        kb_store = MagicMock()
        kb_store.find_applicable.return_value = _kb_entry([
            {
                "find": "import org.apache.log4j.Logger;",
                "replace": "import org.apache.logging.log4j.LogManager;",
                "description": "log4j 1.x to 2.x Logger import change",
            }
        ])
        fixer = _make_fixer(repo_dir, [_end_turn_response(RATIONALE_NO_CHANGES)], kb_store=kb_store)

        with patch.object(fixer._framework, "build") as mock_build:
            mock_build.return_value = MagicMock(success=False, output="compile error")
            summary = _run(fixer)

        assert summary.kb_hit is False
        # The pattern's edit was already applied to disk before the build gate ran.
        content = (Path(repo_dir) / "src/main/java/com/example/App.java").read_text()
        assert "LogManager" in content
        # The model call received the KB entry as context.
        sent_prompt = fixer._client.messages.create.call_args_list[0].kwargs["messages"][0]["content"]
        assert "Knowledge base context" in sent_prompt

    def test_no_kb_store_configured_skips_fast_path(self, repo_dir):
        fixer = _make_fixer(repo_dir, [_end_turn_response(RATIONALE_NO_CHANGES)], kb_store=None)
        summary = _run(fixer)
        assert summary.kb_hit is False
