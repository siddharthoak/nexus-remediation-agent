"""
TEST-03: Unit tests for retry_controller.py.

Tests the bounded-retry safety properties. All model calls, git push, and CI
re-triggering are mocked — no real API calls or git operations are made.

Test names are deliberately descriptive so a reviewer can identify which
safety property each test is protecting without reading the implementation.
"""

import json
import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "agents", "watcher"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "agents", "fixer"))

from retry_controller import (
    RetryController,
    RetryLimitExceededError,
    InMemoryAttemptCounter,
)
from ci_status import CIResult, CIOutcome, FailedCheck


# ── Fixtures ──────────────────────────────────────────────────────────────────

MOCK_DIAGNOSIS_UPGRADE_CAUSED = {
    "caused_by_upgrade": True,
    "diagnosis": "The method Logger.getLogger() was removed in log4j 2.x.",
    "files_to_change": [
        {
            "relative_path": "src/main/java/com/example/App.java",
            "change_description": "Update Logger API",
            "find": "import org.apache.log4j.Logger;",
            "replace": "import org.apache.logging.log4j.LogManager;\nimport org.apache.logging.log4j.Logger;",
        }
    ],
}

MOCK_DIAGNOSIS_UNRELATED_FAILURE = {
    "caused_by_upgrade": False,
    "diagnosis": "Test failure is a pre-existing network timeout in DatabaseTest, unrelated to log4j.",
    "files_to_change": [],
}


def _make_ci_result(pr_number: int = 1, failure_log: str = "Build failed: cannot find symbol") -> CIResult:
    fc = FailedCheck(
        name="build", check_run_id=42, conclusion="failure",
        details_url="https://github.com/org/repo/actions/runs/1",
        log_text=failure_log,
    )
    return CIResult(
        status=CIOutcome.FAILURE,
        pr_number=pr_number,
        head_sha="abc123def456",
        check_run_url="https://github.com/org/repo/actions/runs/1",
        failed_checks=[fc],
    )


_APP_JAVA_ORIGINAL = "import org.apache.log4j.Logger;\npublic class App { }"


def _reset_app_java(repo_dir: str) -> None:
    """
    Restore App.java to its pre-fix content.

    MOCK_DIAGNOSIS_UPGRADE_CAUSED carries a fixed `find` string. In production each retry
    gets a fresh model diagnosis reflecting the file's current (already-partially-fixed)
    state; these tests reuse one canned diagnosis across several attempt_fix() calls to
    isolate counter/bound behavior, so the file must be reset between calls or the second
    call's find-string legitimately won't match anymore (it was already replaced by the
    first call) — that's not a bug, it's apply_diagnosis_changes() correctly refusing to
    reapply a stale patch.
    """
    app_java = Path(repo_dir) / "src" / "main" / "java" / "com" / "example" / "App.java"
    app_java.write_text(_APP_JAVA_ORIGINAL, encoding="utf-8")


@pytest.fixture
def repo_dir():
    tmpdir = tempfile.mkdtemp(prefix="test-watcher-")
    src = Path(tmpdir) / "src" / "main" / "java" / "com" / "example"
    src.mkdir(parents=True)
    (src / "App.java").write_text(_APP_JAVA_ORIGINAL, encoding="utf-8")
    yield tmpdir
    shutil.rmtree(tmpdir, ignore_errors=True)


def _make_controller(repo_dir: str, model_response: dict, max_retries: int = 3):
    """Build a RetryController with all external dependencies mocked."""
    mock_pr_client = MagicMock()
    mock_repo_ops = MagicMock()
    counter = InMemoryAttemptCounter()

    controller = RetryController(
        repo_path=repo_dir,
        pr_client=mock_pr_client,
        repo_ops=mock_repo_ops,
        model_deployment_name="test-model",
        max_retry_attempts=max_retries,
        attempt_counter=counter,
    )

    # Mock the model call to return our canned diagnosis
    mock_msg = MagicMock()
    mock_msg.content = [MagicMock(text=f"```json\n{json.dumps(model_response)}\n```")]
    controller._client = MagicMock()
    controller._client.messages.create.return_value = mock_msg

    return controller, mock_repo_ops, mock_pr_client, counter


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestSingleFailureFix:
    """A single CI failure triggers model diagnosis and pushes to the same branch."""

    def test_model_is_called_once_on_ci_failure(self, repo_dir):
        controller, _, _, _ = _make_controller(repo_dir, MOCK_DIAGNOSIS_UPGRADE_CAUSED)
        controller.attempt_fix(_make_ci_result(pr_number=1), branch_name="fix/log4j-abc123")

        assert controller._client.messages.create.call_count == 1

    def test_fix_is_pushed_to_the_same_branch_not_a_new_one(self, repo_dir):
        """
        SAFETY PROPERTY: The fix is pushed to the existing branch.
        A new branch must NEVER be created by the retry controller.
        """
        controller, mock_repo_ops, _, _ = _make_controller(repo_dir, MOCK_DIAGNOSIS_UPGRADE_CAUSED)
        branch_name = "fix/log4j-abc123"
        controller.attempt_fix(_make_ci_result(pr_number=1), branch_name=branch_name)

        # push_branch must be called with the SAME branch name that was passed in
        mock_repo_ops.push_branch.assert_called_once_with(branch_name)

    def test_commit_is_made_to_same_branch(self, repo_dir):
        controller, mock_repo_ops, _, _ = _make_controller(repo_dir, MOCK_DIAGNOSIS_UPGRADE_CAUSED)
        controller.attempt_fix(_make_ci_result(pr_number=1), branch_name="fix/log4j-abc123")

        mock_repo_ops.commit_changes.assert_called_once()

    def test_returns_true_when_fix_is_applied(self, repo_dir):
        controller, _, _, _ = _make_controller(repo_dir, MOCK_DIAGNOSIS_UPGRADE_CAUSED)
        result = controller.attempt_fix(_make_ci_result(pr_number=1), branch_name="fix/log4j-abc123")

        assert result is True


class TestRetryCountIncrementsCorrectly:
    """
    SAFETY PROPERTY: The attempt counter increments across repeated failures.
    The controller must accurately track how many times it has tried so it can stop.
    """

    def test_attempt_count_increments_on_each_failure(self, repo_dir):
        controller, _, _, counter = _make_controller(repo_dir, MOCK_DIAGNOSIS_UPGRADE_CAUSED, max_retries=5)
        pr_number = 10

        controller.attempt_fix(_make_ci_result(pr_number=pr_number), branch_name="fix/test")
        assert counter.get(pr_number) == 1

        _reset_app_java(repo_dir)
        controller.attempt_fix(_make_ci_result(pr_number=pr_number), branch_name="fix/test")
        assert counter.get(pr_number) == 2

        _reset_app_java(repo_dir)
        controller.attempt_fix(_make_ci_result(pr_number=pr_number), branch_name="fix/test")
        assert counter.get(pr_number) == 3

    def test_attempt_count_is_per_pr_not_global(self, repo_dir):
        """Different PRs must have independent counters."""
        controller, _, _, counter = _make_controller(repo_dir, MOCK_DIAGNOSIS_UPGRADE_CAUSED, max_retries=5)

        controller.attempt_fix(_make_ci_result(pr_number=1), branch_name="fix/a")
        _reset_app_java(repo_dir)
        controller.attempt_fix(_make_ci_result(pr_number=2), branch_name="fix/b")
        _reset_app_java(repo_dir)
        controller.attempt_fix(_make_ci_result(pr_number=1), branch_name="fix/a")

        assert counter.get(1) == 2
        assert counter.get(2) == 1


class TestHardRetryBound:
    """
    SAFETY PROPERTY: The controller stops attempting fixes once MAX_RETRY_ATTEMPTS is reached.
    This is the most critical safety test in the suite.
    """

    def test_controller_stops_after_max_attempts(self, repo_dir):
        """
        Given MAX_RETRY_ATTEMPTS=3 and 4 consecutive failures, the 4th call must NOT
        attempt a fix. It must raise RetryLimitExceededError.
        """
        controller, mock_repo_ops, _, counter = _make_controller(
            repo_dir, MOCK_DIAGNOSIS_UPGRADE_CAUSED, max_retries=3
        )
        pr_number = 99

        # Attempts 1, 2, 3 should succeed
        controller.attempt_fix(_make_ci_result(pr_number=pr_number), branch_name="fix/test")
        _reset_app_java(repo_dir)
        controller.attempt_fix(_make_ci_result(pr_number=pr_number), branch_name="fix/test")
        _reset_app_java(repo_dir)
        controller.attempt_fix(_make_ci_result(pr_number=pr_number), branch_name="fix/test")

        assert counter.get(pr_number) == 3
        assert mock_repo_ops.push_branch.call_count == 3

        # 4th attempt: must raise, must NOT call model or push
        with pytest.raises(RetryLimitExceededError):
            controller.attempt_fix(_make_ci_result(pr_number=pr_number), branch_name="fix/test")

        # Model and push must NOT have been called a 4th time
        assert controller._client.messages.create.call_count == 3
        assert mock_repo_ops.push_branch.call_count == 3

    def test_limit_is_respected_even_when_called_again_after_already_reached(self, repo_dir):
        """
        SAFETY PROPERTY: The bound cannot be bypassed by calling attempt_fix() again
        after the limit was already hit. The controller must refuse every subsequent call.
        """
        controller, mock_repo_ops, _, counter = _make_controller(
            repo_dir, MOCK_DIAGNOSIS_UPGRADE_CAUSED, max_retries=2
        )
        pr_number = 77

        controller.attempt_fix(_make_ci_result(pr_number=pr_number), branch_name="fix/test")
        _reset_app_java(repo_dir)
        controller.attempt_fix(_make_ci_result(pr_number=pr_number), branch_name="fix/test")

        # Now at limit — next 3 calls must all raise and must not push
        for _ in range(3):
            with pytest.raises(RetryLimitExceededError):
                controller.attempt_fix(_make_ci_result(pr_number=pr_number), branch_name="fix/test")

        assert mock_repo_ops.push_branch.call_count == 2  # Only the legitimate attempts


class TestEscalationOnLimitReached:
    """
    SAFETY PROPERTY: When the retry limit is reached, the PR receives an escalation comment.
    The developer must be notified that human review is needed.
    """

    def test_escalation_comment_is_posted_when_limit_is_reached(self, repo_dir):
        controller, _, mock_pr_client, _ = _make_controller(
            repo_dir, MOCK_DIAGNOSIS_UPGRADE_CAUSED, max_retries=1
        )
        pr_number = 55
        controller.attempt_fix(_make_ci_result(pr_number=pr_number), branch_name="fix/test")

        # After exhausting the limit, the pr_client should have been asked to post a comment
        mock_pr_client.add_comment.assert_called()
        comment_text = mock_pr_client.add_comment.call_args[0][1]
        assert "retry" in comment_text.lower() or "human" in comment_text.lower()


class TestUnrelatedFailureHandling:
    """
    When CI failure is unrelated to the upgrade, the controller must NOT retry.
    Retrying an unrelated failure wastes cycles and could hide the real problem.
    """

    def test_returns_false_for_unrelated_failure(self, repo_dir):
        controller, _, _, _ = _make_controller(repo_dir, MOCK_DIAGNOSIS_UNRELATED_FAILURE)
        result = controller.attempt_fix(_make_ci_result(pr_number=1), branch_name="fix/test")

        assert result is False

    def test_no_push_for_unrelated_failure(self, repo_dir):
        """
        SAFETY PROPERTY: No code is committed or pushed when the failure is unrelated.
        """
        controller, mock_repo_ops, _, _ = _make_controller(
            repo_dir, MOCK_DIAGNOSIS_UNRELATED_FAILURE
        )
        controller.attempt_fix(_make_ci_result(pr_number=1), branch_name="fix/test")

        mock_repo_ops.push_branch.assert_not_called()
        mock_repo_ops.commit_changes.assert_not_called()

    def test_escalation_comment_is_posted_for_unrelated_failure(self, repo_dir):
        controller, _, mock_pr_client, _ = _make_controller(
            repo_dir, MOCK_DIAGNOSIS_UNRELATED_FAILURE
        )
        controller.attempt_fix(_make_ci_result(pr_number=5), branch_name="fix/test")

        mock_pr_client.add_comment.assert_called_once()

    def test_attempt_counter_does_not_increment_for_unrelated_failure(self, repo_dir):
        """Unrelated failures must not consume retry budget."""
        controller, _, _, counter = _make_controller(
            repo_dir, MOCK_DIAGNOSIS_UNRELATED_FAILURE, max_retries=3
        )
        pr_number = 88
        controller.attempt_fix(_make_ci_result(pr_number=pr_number), branch_name="fix/test")

        assert counter.get(pr_number) == 0
