"""
Fixer Agent entry point.

Exactly two trigger modes — no others are valid:

  Mode A — FRESH SCAN (scheduler trigger):
      RETRY_TRACKING_ID env var is NOT set.
      Fetches Nexus IQ vulnerability report, fixes each vulnerability in parallel,
      opens PRs, and records the full attempt lifecycle in the tracking store.

  Mode B — WATCHER RETRY (Watcher-triggered):
      RETRY_TRACKING_ID env var IS set by the Watcher when it invokes this container
      with a specific tracking_id for a RETRY_REQUESTED record.
      Validates the tracking record, applies the fix to the existing PR branch,
      pushes the commit. Never opens a new PR.

The mode selection happens entirely in main() by checking RETRY_TRACKING_ID.
The CodeFixer class enforces the validation logic — main() just routes.

Parallelism (Mode A only):
  The repo is cloned once from GitHub, then each vulnerability fix runs in its own
  thread using a fast local copy (RepoOps.clone_local). The bottleneck is Claude API
  calls (~5-15 s each) — running MAX_PARALLEL_FIXES workers concurrently amortises
  that cost across all findings. PR creation and tracking updates run sequentially
  in the main thread after each worker finishes.

Thread-safety note:
  tracking_store.update() is called from worker threads to record token usage.
  CosmosTrackingStore (production) is safe — each call is an independent network
  request. InMemoryTrackingStore (testing) relies on CPython's GIL for dict safety,
  which is sufficient for a PoC but not production-grade.
"""

import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

from github import Github
from nexus_client import NexusIQClient, NexusAuthError
from repo_ops import RepoOps
from code_fixer import CodeFixer, InvalidRetryError
from pr_client import PRClient

# common/ is on the path (see Dockerfile COPY)
from common.tracking_store import (
    make_tracking_store,
    make_fresh_record,
    TrackingStatus,
)
from common.knowledge_store import make_knowledge_store
from knowledge_agent.main import KnowledgeAgent
from classifier.rules import Classifier

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("fixer.main")

MAX_PARALLEL_FIXES = int(os.environ.get("MAX_PARALLEL_FIXES", "5"))


def main():
    retry_tracking_id = os.environ.get("RETRY_TRACKING_ID")

    if retry_tracking_id:
        _run_retry(retry_tracking_id)
    else:
        _run_fresh_scan()


# ── Mode A: fresh scan ────────────────────────────────────────────────────────

def _run_fresh_scan():
    """
    Scheduler-triggered path. Clones the repo once, fixes all vulnerabilities in
    parallel (one thread per finding), opens PRs, and records state in the tracking store.
    """
    logger.info("Mode: FRESH SCAN (scheduler-triggered)")

    nexus_app_id    = os.environ["NEXUS_IQ_APP_PUBLIC_ID"]
    github_repo     = os.environ["GITHUB_REPO_TARGET"]
    github_repo_url = f"https://github.com/{github_repo}.git"
    github_pat      = os.environ["GITHUB_PAT"]

    tracking_store = make_tracking_store()

    nexus = NexusIQClient()
    try:
        findings = nexus.get_vulnerability_report(nexus_app_id)
    except NexusAuthError as exc:
        logger.error("Nexus IQ auth failed: %s", exc)
        sys.exit(1)

    if not findings:
        logger.info("No vulnerabilities found for %s. Nothing to do.", nexus_app_id)
        return

    logger.info("Found %d vulnerability finding(s).", len(findings))

    pr_client   = PRClient(repo_full_name=github_repo, github_pat=github_pat)
    base_branch = Github(github_pat).get_repo(github_repo).default_branch

    # ── Phase 2: KB hydration + classification ────────────────────────────────
    kb_store        = make_knowledge_store()
    knowledge_agent = KnowledgeAgent(github_pat=github_pat)
    knowledge_agent.hydrate(findings, kb_store)

    classifier     = Classifier(kb_store=kb_store)
    classification = {}  # component_name → ClassifierResult
    for finding in findings:
        result = classifier.classify(finding)
        classification[finding.component_name] = result
        logger.info(
            "Classifier: %s → bucket %d (%s)",
            finding.component_name, result.bucket, result.rationale,
        )
        if result.bucket in (1, 4):
            logger.info(
                "Bucket %d — opening triage issue for %s.",
                result.bucket, finding.component_name,
            )
            pr_client.open_triage_issue(
                finding=finding,
                bucket=result.bucket,
                rationale=result.rationale,
                kb_entry=result.kb_entry,
            )
    # ─────────────────────────────────────────────────────────────────────────

    # ── Clone once — all parallel workers copy from this local source ─────────
    source_repo = RepoOps()
    source_path = source_repo.clone(github_repo_url, github_pat)
    logger.info(
        "Source clone ready at %s — up to %d parallel fixes will copy from here.",
        source_path, MAX_PARALLEL_FIXES,
    )

    # ── Create tracking records before parallelism (sequential, avoids store races) ──
    tasks = []
    for finding in findings:
        result = classification[finding.component_name]
        if result.bucket in (1, 4):
            continue  # triage issue already created above

        branch_name = RepoOps.make_branch_name(finding.component_name, finding.current_version)
        record = make_fresh_record(
            vulnerability_id=finding.cve_ids[0] if finding.cve_ids else finding.component_name,
            repo=github_repo,
            component_name=finding.component_name,
            old_version=finding.current_version,
            new_version=finding.recommended_version,
        )
        record.branch_name = branch_name
        tracking_store.create(record)
        tasks.append((finding, branch_name, record, result.kb_entry))

    # ── Fix each vulnerability in parallel ────────────────────────────────────

    def _fix_one(task):
        finding, branch_name, record, kb_entry = task
        logger.info("Processing %s → branch %s", finding.component_name, branch_name)

        with RepoOps() as repo:
            # Fast local copy — no GitHub round-trip per finding
            repo.clone_local(source_path, github_repo_url, github_pat)

            branch_created = repo.create_branch(branch_name, skip_if_exists=True)
            if not branch_created:
                logger.info(
                    "Branch already exists — PR likely open. Skipping %s.",
                    finding.component_name,
                )
                return None

            fixer = CodeFixer(repo_path=repo._local_path)
            try:
                summary = fixer.run_fresh_fix(
                    component_name=finding.component_name,
                    current_version=finding.current_version,
                    target_version=finding.recommended_version,
                    tracking_id=record.tracking_id,
                    tracking_store=tracking_store,
                    cve_ids=finding.cve_ids,
                    kb_entry=kb_entry,
                )
            except Exception as exc:
                logger.error("Fix failed for %s: %s", finding.component_name, exc)
                return None

            commit_msg = (
                f"fix: upgrade {finding.component_name} to {finding.recommended_version}"
                + (f" ({', '.join(finding.cve_ids)})" if finding.cve_ids else "")
            )
            repo.commit_changes(commit_msg)
            repo.push_branch(branch_name)

        return (finding, branch_name, record, summary)

    try:
        with ThreadPoolExecutor(max_workers=MAX_PARALLEL_FIXES) as executor:
            futures = {executor.submit(_fix_one, t): t for t in tasks}

            # Process results as each worker finishes — PR creation and tracking
            # updates run here in the main thread, sequentially.
            for future in as_completed(futures):
                result = future.result()
                if result is None:
                    continue

                finding, branch_name, record, summary = result

                pr_result = pr_client.open_remediation_pr(
                    branch_name=branch_name,
                    base_branch=base_branch,
                    change_summary=summary,
                )

                record = tracking_store.get(record.tracking_id)
                record.pr_number = pr_result.pr_number
                record.status = TrackingStatus.PR_OPENED.value
                tracking_store.update(record)

                if pr_result.was_existing:
                    logger.info("PR already existed: %s", pr_result.pr_url)
                else:
                    logger.info("Opened PR #%d: %s", pr_result.pr_number, pr_result.pr_url)
                    record.status = TrackingStatus.CI_PENDING.value
                    tracking_store.update(record)

    finally:
        source_repo.cleanup()
        logger.info("Source clone cleaned up.")


# ── Mode B: Watcher retry ─────────────────────────────────────────────────────

def _run_retry(tracking_id: str):
    """
    Watcher-triggered retry path. Applies a fix to an existing PR branch
    and pushes it. Never opens a new PR. Never creates a new branch.

    The CodeFixer.run_retry_fix() validates that:
      - The tracking record exists
      - Its status is RETRY_REQUESTED (only the Watcher sets this)
      - attempt_number <= MAX_RETRY_ATTEMPTS
    If any check fails, InvalidRetryError is raised and we exit non-zero.
    """
    logger.info("Mode: WATCHER RETRY (tracking_id=%s)", tracking_id[:8])

    github_repo     = os.environ["GITHUB_REPO_TARGET"]
    github_repo_url = f"https://github.com/{github_repo}.git"
    github_pat      = os.environ["GITHUB_PAT"]

    tracking_store = make_tracking_store()

    record = tracking_store.get(tracking_id)
    if record is None:
        logger.error("Tracking record %s not found. Exiting.", tracking_id[:8])
        sys.exit(1)

    if not record.branch_name:
        logger.error(
            "Tracking record %s has no branch_name — cannot check out the PR branch.",
            tracking_id[:8],
        )
        sys.exit(1)

    with RepoOps() as repo:
        repo.clone(github_repo_url, github_pat)
        # Check out the EXISTING PR branch — never create a new one
        repo._repo.git.checkout(record.branch_name)

        fixer = CodeFixer(repo_path=repo._local_path)
        try:
            summary = fixer.run_retry_fix(
                tracking_id=tracking_id,
                tracking_store=tracking_store,
            )
        except InvalidRetryError as exc:
            logger.error("Retry validation failed: %s", exc)
            sys.exit(1)

        commit_msg = (
            f"fix(retry): attempt {record.attempt_number} — "
            f"{summary.rationale[:120] if summary.rationale else 'CI failure fix'}"
        )
        repo.commit_changes(commit_msg)
        repo.push_branch(record.branch_name)
        # GitHub Actions re-triggers automatically on this push — no explicit API call needed.

    record = tracking_store.get(tracking_id)
    record.status = TrackingStatus.CI_PENDING.value
    tracking_store.update(record)

    logger.info(
        "Retry fix pushed for PR #%s on branch '%s'.",
        record.pr_number, record.branch_name,
    )


if __name__ == "__main__":
    main()
