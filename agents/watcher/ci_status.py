"""
WAT-01: CI status poller — monitor a PR's CI checks and retrieve failure logs.

Uses the GitHub Checks API (not the legacy Commit Status API) because GitHub Actions
and most modern CI integrations report via Checks, which provides structured log access.
The legacy Commit Status API only gives a pass/fail URL and cannot retrieve log content,
which the Watcher needs to reason about failures.
"""

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from github import Github, GithubException

logger = logging.getLogger(__name__)


class CIOutcome(Enum):
    SUCCESS = "success"
    FAILURE = "failure"
    TIMEOUT = "timeout"


@dataclass
class FailedCheck:
    name: str
    check_run_id: int
    conclusion: str
    details_url: str
    log_text: str = ""


@dataclass
class CIResult:
    status: CIOutcome
    pr_number: int
    head_sha: str
    check_run_url: Optional[str] = None
    failed_checks: list = field(default_factory=list)

    @property
    def failure_log_text(self) -> str:
        """Concatenated log text from all failed check runs, for LLM reasoning."""
        if not self.failed_checks:
            return ""
        parts = []
        for fc in self.failed_checks:
            parts.append(f"=== Check: {fc.name} (conclusion: {fc.conclusion}) ===")
            parts.append(fc.log_text or "(no log text retrieved)")
        return "\n\n".join(parts)


class CIStatusWatcher:
    """
    Polls the GitHub Checks API for a pull request until CI reaches a terminal state.

    Handles multiple check runs on the same commit (e.g. build + unit tests + integration
    tests as separate checks). The overall result is:
      - SUCCESS only if ALL required checks pass.
      - FAILURE if ANY required check fails.
      - TIMEOUT if the terminal state isn't reached within `timeout_seconds`.
    """

    # Check run conclusions that indicate a terminal failure (not just pending/queued)
    FAILURE_CONCLUSIONS = {"failure", "timed_out", "action_required", "cancelled", "stale"}
    SUCCESS_CONCLUSIONS = {"success", "skipped", "neutral"}

    def __init__(self, repo_full_name: str, github_pat: str):
        self._repo_full_name = repo_full_name
        gh = Github(github_pat)
        self._repo = gh.get_repo(repo_full_name)

    def wait_for_ci(
        self,
        pr_number: int,
        poll_interval_seconds: int = 30,
        timeout_seconds: int = 1800,
    ) -> CIResult:
        """
        Poll until all CI check runs on the PR's head commit reach a terminal state,
        or until `timeout_seconds` is exceeded.

        poll_interval_seconds and timeout_seconds are intentionally configurable since
        CI run durations vary enormously across projects and need tuning in practice.
        """
        pr = self._repo.get_pull(pr_number)
        head_sha = pr.head.sha

        logger.info(
            "Watching CI for PR #%d (sha %s), timeout=%ds, interval=%ds",
            pr_number, head_sha[:8], timeout_seconds, poll_interval_seconds,
        )

        elapsed = 0
        while elapsed < timeout_seconds:
            result = self._evaluate_check_runs(pr_number, head_sha)
            if result is not None:
                return result

            logger.debug(
                "PR #%d: CI still in progress. Waiting %ds (elapsed %ds/%ds)...",
                pr_number, poll_interval_seconds, elapsed, timeout_seconds,
            )
            time.sleep(poll_interval_seconds)
            elapsed += poll_interval_seconds

        logger.warning("PR #%d: CI did not complete within %ds — timeout.", pr_number, timeout_seconds)
        return CIResult(
            status=CIOutcome.TIMEOUT,
            pr_number=pr_number,
            head_sha=head_sha,
        )

    # ── Private helpers ───────────────────────────────────────────────────────

    def _evaluate_check_runs(self, pr_number: int, head_sha: str) -> Optional[CIResult]:
        """
        Inspect the current state of all check runs on `head_sha`.

        Returns a CIResult if all check runs have reached a terminal state, or None
        if any check is still in a pending/queued/in_progress state.
        """
        try:
            commit = self._repo.get_commit(head_sha)
            check_runs = list(commit.get_check_runs())
        except GithubException as exc:
            logger.warning("GitHub API error fetching check runs: %s", exc)
            return None

        if not check_runs:
            # No checks registered yet — CI hasn't started; keep waiting
            return None

        in_progress_statuses = {"queued", "in_progress"}
        all_terminal = all(cr.status not in in_progress_statuses for cr in check_runs)

        if not all_terminal:
            return None  # At least one check still running

        # All check runs are terminal — classify
        failed_checks = []
        for cr in check_runs:
            if cr.conclusion in self.FAILURE_CONCLUSIONS:
                log_text = self._fetch_log(cr)
                failed_checks.append(FailedCheck(
                    name=cr.name,
                    check_run_id=cr.id,
                    conclusion=cr.conclusion,
                    details_url=cr.details_url or "",
                    log_text=log_text,
                ))

        if failed_checks:
            logger.info(
                "PR #%d: CI FAILED — %d check(s) failed: %s",
                pr_number,
                len(failed_checks),
                [fc.name for fc in failed_checks],
            )
            return CIResult(
                status=CIOutcome.FAILURE,
                pr_number=pr_number,
                head_sha=head_sha,
                check_run_url=failed_checks[0].details_url,
                failed_checks=failed_checks,
            )

        logger.info("PR #%d: CI PASSED — all %d check(s) succeeded.", pr_number, len(check_runs))
        return CIResult(
            status=CIOutcome.SUCCESS,
            pr_number=pr_number,
            head_sha=head_sha,
        )

    def _fetch_log(self, check_run) -> str:
        """
        Fetch the log output for a failed check run.

        GitHub's Checks API does not expose full log text directly on the check run object;
        it is available via the annotations or the `output` field summary/text properties.
        For GitHub Actions specifically, full job logs require the Actions API.

        We use output.text + output.summary as the primary signal, which is what most
        CI integrations populate and is sufficient for LLM-based failure diagnosis.
        """
        try:
            output = check_run.output
            parts = []
            if output.title:
                parts.append(f"Title: {output.title}")
            if output.summary:
                parts.append(f"Summary:\n{output.summary}")
            if output.text:
                parts.append(f"Detail:\n{output.text}")
            return "\n\n".join(parts) if parts else ""
        except Exception as exc:
            logger.warning("Could not fetch log for check run %d: %s", check_run.id, exc)
            return ""
