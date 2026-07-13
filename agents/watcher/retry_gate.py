"""
Watcher retry gate — decides whether to request a retry and invokes the Fixer.

This module contains ZERO model calls, ZERO code editing, and ZERO git operations.
Those responsibilities belong exclusively to the Fixer. This gate does three things:

  1. Check the retry bound: count existing attempts for this PR against MAX_RETRY_ATTEMPTS.
  2. If the bound has NOT been reached:
       - Create a new RETRY_REQUESTED tracking record (with failure_log_excerpt set so
         the Fixer's next run is an INFORMED retry, not a blind one).
       - Invoke the Fixer via the Fixer invoker (which triggers a new Fixer container run
         with RETRY_TRACKING_ID set to the new tracking_id).
  3. If the bound HAS been reached (or failure is unrelated/unclassifiable):
       - Write FAILED_MAX_RETRIES status to the tracking record.
       - Post an escalation comment on the PR.
       - Stop. Do NOT call the Fixer under any circumstances after this point.

Safety invariant enforced here:
  Once FAILED_MAX_RETRIES is written, this gate refuses to create any further
  RETRY_REQUESTED records for the same PR, regardless of how many times it is called.
  The Fixer has its own independent validation layer (InvalidRetryError) as a second
  line of defence, but the primary enforcement is here.
"""

import logging
import os
from datetime import datetime, timezone
from typing import Optional

from common.tracking_store import TrackingStatus, make_retry_record
from common.telemetry import emit_event

logger = logging.getLogger(__name__)


class RetryGate:
    """
    The Watcher's sole decision-making component for CI failures.
    Reads/writes the tracking store and invokes the Fixer — nothing else.
    """

    def __init__(
        self,
        tracking_store,      # TrackingStoreProtocol
        pr_client,           # pr_client.PRClient
        fixer_invoker,       # FixerInvoker
        max_retry_attempts: Optional[int] = None,
    ):
        self._store = tracking_store
        self._pr_client = pr_client
        self._invoker = fixer_invoker
        self._max_attempts = max_retry_attempts or int(os.environ.get("MAX_RETRY_ATTEMPTS", "3"))

    def process_ci_failure(self, ci_result, current_tracking_record) -> None:
        """
        Entry point called by watcher/main.py when CI has failed on a remediation PR.

        `ci_result`               — CIResult from ci_status.py (contains failure logs)
        `current_tracking_record` — the most recent TrackingRecord for this PR
        """
        pr_number = current_tracking_record.pr_number
        if pr_number is None:
            logger.error(
                "Tracking record %s has no pr_number — cannot process CI failure.",
                current_tracking_record.tracking_id[:8],
            )
            return

        # ── Guard: refuse if record is already at a terminal failure state ────
        terminal_statuses = {
            TrackingStatus.FAILED_MAX_RETRIES.value,
            TrackingStatus.ESCALATED.value,
        }
        if current_tracking_record.status in terminal_statuses:
            logger.error(
                "PR #%d: tracking record already has terminal status=%s. "
                "Refusing to create any further retry requests.",
                pr_number, current_tracking_record.status,
            )
            return

        # ── Count total attempts already recorded for this PR ─────────────────
        attempt_count = self._store.count_attempts_for_pr(pr_number)

        logger.info(
            "PR #%d: CI failed. Attempt %d/%d completed.",
            pr_number, attempt_count, self._max_attempts,
        )

        if attempt_count >= self._max_attempts:
            self._handle_limit_reached(pr_number, current_tracking_record, ci_result)
            return

        # ── Request retry: write new RETRY_REQUESTED record ───────────────────
        # The failure log excerpt is written here so the Fixer's next run is informed.
        failure_excerpt = ci_result.failure_log_text
        if not failure_excerpt:
            logger.warning(
                "PR #%d: CI result has no failure log text. "
                "Retry will have reduced context.", pr_number
            )

        retry_record = make_retry_record(
            parent=current_tracking_record,
            failure_log_excerpt=failure_excerpt,
        )
        self._store.create(retry_record)

        logger.info(
            "PR #%d: created RETRY_REQUESTED record %s (attempt %d/%d).",
            pr_number, retry_record.tracking_id[:8],
            retry_record.attempt_number, self._max_attempts,
        )

        # ── Invoke Fixer with the new tracking_id ─────────────────────────────
        # The Fixer will validate the record status before acting.
        try:
            self._invoker.trigger_retry(retry_record.tracking_id)
            logger.info(
                "PR #%d: Fixer invoked with tracking_id=%s.",
                pr_number, retry_record.tracking_id[:8],
            )
            emit_event(
                "FixRetryRequested",
                tracking_id=retry_record.tracking_id,
                parent_tracking_id=retry_record.parent_tracking_id,
                repo=retry_record.repo,
                component_name=retry_record.component_name,
                pr_number=pr_number,
                attempt_number=retry_record.attempt_number,
                max_retry_attempts=self._max_attempts,
            )
        except Exception as exc:
            # If we can't invoke the Fixer, mark the record as failed so the
            # Watcher doesn't create another RETRY_REQUESTED on the next poll cycle.
            logger.error(
                "PR #%d: Failed to invoke Fixer: %s. Marking as ESCALATED.", pr_number, exc
            )
            retry_record.status = TrackingStatus.ESCALATED.value
            self._store.update(retry_record)
            emit_event(
                "FixEscalated",
                tracking_id=retry_record.tracking_id,
                repo=retry_record.repo,
                component_name=retry_record.component_name,
                pr_number=pr_number,
                attempt_number=retry_record.attempt_number,
                reason="fixer_invocation_failed",
            )
            self._post_escalation_comment(
                pr_number,
                f"The Watcher agent could not invoke the Fixer for retry "
                f"(tracking={retry_record.tracking_id[:8]}): {exc}\n\n"
                "Human intervention required."
            )

    # ── Private helpers ───────────────────────────────────────────────────────

    def _handle_limit_reached(self, pr_number: int, record, ci_result) -> None:
        """Write FAILED_MAX_RETRIES and escalate to human. Never invokes the Fixer."""
        logger.warning(
            "PR #%d: MAX_RETRY_ATTEMPTS=%d reached. Stopping all automatic retries.",
            pr_number, self._max_attempts,
        )

        try:
            created_dt = datetime.fromisoformat(record.created_at)
            now = datetime.now(timezone.utc)
            resolution_seconds = (now - created_dt).total_seconds()
        except Exception:
            resolution_seconds = None

        record.status = TrackingStatus.FAILED_MAX_RETRIES.value
        record.time_to_resolution_seconds = resolution_seconds
        self._store.update(record)
        emit_event(
            "FixEscalated",
            tracking_id=record.tracking_id,
            repo=record.repo,
            component_name=record.component_name,
            old_version=record.old_version,
            new_version=record.new_version,
            pr_number=pr_number,
            attempt_number=record.attempt_number,
            max_retry_attempts=self._max_attempts,
            time_to_resolution_seconds=resolution_seconds,
            reason="max_retries_reached",
        )

        self._post_escalation_comment(
            pr_number,
            f"## OSS Remediation Agent — Retry Limit Reached\n\n"
            f"This PR has exhausted all **{self._max_attempts}** automatic fix attempts. "
            f"No further automatic fixes will be applied.\n\n"
            f"**Latest CI failure:**\n```\n{ci_result.failure_log_text[:1000]}\n```\n\n"
            "Please investigate the CI failure and apply a manual fix before merging."
        )

    def _post_escalation_comment(self, pr_number: int, comment: str) -> None:
        try:
            self._pr_client.add_comment(pr_number, comment)
        except Exception as exc:
            logger.error(
                "Could not post escalation comment on PR #%d: %s", pr_number, exc
            )


# ── Fixer invoker ────────────────────────────────────────────────────────────
# Abstraction over "how the Watcher triggers a new Fixer container run."
# The Fixer reads RETRY_TRACKING_ID at startup to detect it is in retry mode.

class AafFixerInvoker:
    """
    Triggers a new Fixer agent run via Azure AI Foundry's agent invocation API,
    passing the tracking_id via the RETRY_TRACKING_ID environment override.

    NOTE: The exact AIProjectClient method for triggering an agent run with env var
    overrides must be confirmed against current AAF SDK documentation before relying
    on this. The pattern below is based on expected SDK behaviour; flag any discrepancy.
    """

    def __init__(self, fixer_agent_id: Optional[str] = None):
        self._fixer_agent_id = fixer_agent_id or os.environ["FIXER_AGENT_ID"]

    def trigger_retry(self, tracking_id: str) -> None:
        from azure.ai.projects import AIProjectClient
        from azure.identity import DefaultAzureCredential

        project_endpoint = os.environ["PROJECT_ENDPOINT"]
        client = AIProjectClient(
            endpoint=project_endpoint,
            credential=DefaultAzureCredential(),
        )

        # NOTE: Confirm the method and parameter names for triggering a Hosted Agent run
        # with environment variable overrides in the current azure-ai-projects SDK.
        # The call below is the expected pattern; verify before relying on it.
        client.agents.create_run(
            agent_id=self._fixer_agent_id,
            environment_overrides={"RETRY_TRACKING_ID": tracking_id},
        )
        logger.info(
            "AAF: triggered Fixer agent %s with RETRY_TRACKING_ID=%s",
            self._fixer_agent_id[:8], tracking_id[:8],
        )


class HttpFixerInvoker:
    """
    Alternative invoker for local development: POSTs to the Fixer's HTTP endpoint.
    The Fixer must be running as a server (e.g. `uvicorn main:app`) for this to work.
    Not for production AAF use.
    """

    def __init__(self, fixer_retry_url: Optional[str] = None):
        self._url = fixer_retry_url or os.environ["FIXER_RETRY_URL"]

    def trigger_retry(self, tracking_id: str) -> None:
        import requests
        resp = requests.post(
            self._url,
            json={"tracking_id": tracking_id},
            timeout=10,
        )
        resp.raise_for_status()
        logger.info("HTTP: triggered Fixer retry at %s for tracking_id=%s", self._url, tracking_id[:8])


def make_fixer_invoker():
    """Return the appropriate invoker based on environment."""
    if os.environ.get("FIXER_RETRY_URL"):
        return HttpFixerInvoker()
    return AafFixerInvoker()
