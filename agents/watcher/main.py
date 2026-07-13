"""
Watcher Agent entry point.

Scheduled trigger (every 15 minutes, see watcher.agent.yaml). On each invocation:
  1. Find open remediation PRs (by branch prefix).
  2. For each PR, load the latest tracking record from the store.
  3. Skip PRs already in a terminal state (CI_PASSED, FAILED_MAX_RETRIES, ESCALATED).
  4. Poll CI status via CIStatusWatcher (has its own timeout).
  5. If CI passed → update tracking record to CI_PASSED.
  6. If CI failed → delegate to RetryGate, which checks the retry bound, writes a new
     RETRY_REQUESTED tracking record, and invokes the Fixer container.

The Watcher NEVER:
  - Calls the Anthropic model
  - Writes code or edits files
  - Touches a git repository (no RepoOps, no GitPython, no git binary calls)
  - Merges or force-pushes PRs
"""

import logging
import os
import sys
from datetime import datetime, timezone

from github import Github
from ci_status import CIStatusWatcher, CIOutcome
from pr_client import PRClient
from retry_gate import RetryGate, make_fixer_invoker
from pattern_learner import PatternLearner

from common.tracking_store import make_tracking_store, TrackingStatus
from common.knowledge_store import make_knowledge_store
from common.telemetry import init_telemetry, emit_event, shutdown_telemetry

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("watcher.main")

REMEDIATION_BRANCH_PREFIX = "fix/"

_TERMINAL_STATUSES = {
    TrackingStatus.CI_PASSED.value,
    TrackingStatus.FAILED_MAX_RETRIES.value,
    TrackingStatus.ESCALATED.value,
}


def _elapsed_seconds_since(iso_timestamp: str) -> "float | None":
    """Best-effort elapsed time since an ISO 8601 timestamp; None if unparseable."""
    try:
        created_dt = datetime.fromisoformat(iso_timestamp)
        return (datetime.now(timezone.utc) - created_dt).total_seconds()
    except Exception:
        return None


def find_open_remediation_prs(repo):
    """Return open PRs whose head branch starts with the remediation prefix."""
    pulls = repo.get_pulls(state="open")
    return [pr for pr in pulls if pr.head.ref.startswith(REMEDIATION_BRANCH_PREFIX)]


def main():
    init_telemetry(role_name="watcher")

    github_repo = os.environ["GITHUB_REPO_TARGET"]
    github_pat = os.environ["GITHUB_PAT"]

    gh = Github(github_pat)
    repo = gh.get_repo(github_repo)

    remediation_prs = find_open_remediation_prs(repo)
    if not remediation_prs:
        logger.info("No open remediation PRs found. Nothing to watch.")
        return

    logger.info("Watching %d open remediation PR(s).", len(remediation_prs))

    tracking_store  = make_tracking_store()
    kb_store        = make_knowledge_store()
    ci_watcher      = CIStatusWatcher(repo_full_name=github_repo, github_pat=github_pat)
    pr_client       = PRClient(repo_full_name=github_repo, github_pat=github_pat)
    retry_gate      = RetryGate(
        tracking_store=tracking_store,
        pr_client=pr_client,
        fixer_invoker=make_fixer_invoker(),
    )
    pattern_learner = PatternLearner(
        repo_full_name=github_repo,
        github_pat=github_pat,
    )

    for pr in remediation_prs:
        _process_pr(pr, tracking_store, ci_watcher, retry_gate, kb_store, pattern_learner)


def _process_pr(pr, tracking_store, ci_watcher, retry_gate, kb_store, pattern_learner) -> None:
    pr_number = pr.number

    record = tracking_store.get_latest_for_pr(pr_number)
    if record is None:
        logger.warning(
            "PR #%d: no tracking record found (opened outside the agent?). Skipping.",
            pr_number,
        )
        return

    if record.status in _TERMINAL_STATUSES:
        logger.info("PR #%d: status=%s (terminal). Skipping.", pr_number, record.status)
        return

    logger.info(
        "PR #%d (%s): checking CI. Current status=%s",
        pr_number, pr.head.ref, record.status,
    )

    ci_result = ci_watcher.wait_for_ci(
        pr_number=pr_number,
        poll_interval_seconds=int(os.environ.get("CI_POLL_INTERVAL", "30")),
        timeout_seconds=int(os.environ.get("CI_TIMEOUT_SECONDS", "1800")),
    )

    if ci_result.status == CIOutcome.SUCCESS:
        logger.info("PR #%d: CI passed. Marking resolved.", pr_number)
        record.status = TrackingStatus.CI_PASSED.value
        tracking_store.update(record)
        emit_event(
            "FixResolved",
            tracking_id=record.tracking_id,
            repo=record.repo,
            component_name=record.component_name,
            old_version=record.old_version,
            new_version=record.new_version,
            vulnerability_id=record.vulnerability_id,
            pr_number=pr_number,
            attempt_number=record.attempt_number,
            time_to_resolution_seconds=_elapsed_seconds_since(record.created_at),
        )
        # Tier 1: learn fix patterns from this confirmed-good PR
        try:
            pattern_learner.learn_from_pr(pr_number, record, kb_store)
        except Exception as exc:
            logger.warning("PR #%d: pattern learning failed (non-fatal): %s", pr_number, exc)
        return

    if ci_result.status == CIOutcome.TIMEOUT:
        logger.warning("PR #%d: CI timed out. Will retry on next Watcher cycle.", pr_number)
        return

    # CI failed — write the intermediate status then let RetryGate decide what to do.
    # RetryGate owns: bound check, RETRY_REQUESTED record creation, Fixer invocation.
    # The Watcher does not write code, clone repos, or push commits before or after this call.
    logger.info("PR #%d: CI failed. Delegating to RetryGate.", pr_number)
    record.status = TrackingStatus.CI_FAILED.value
    tracking_store.update(record)

    retry_gate.process_ci_failure(ci_result, current_tracking_record=record)


if __name__ == "__main__":
    try:
        main()
    finally:
        shutdown_telemetry()
