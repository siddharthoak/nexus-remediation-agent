"""
FIX-04: PR client — open a GitHub pull request with a structured remediation description.

Uses PyGitHub for the GitHub REST API. Idempotent: if an open PR already exists for the
branch, returns its details rather than creating a duplicate.
"""

import logging
import os
from dataclasses import dataclass
from typing import Optional

from github import Github, GithubException

logger = logging.getLogger(__name__)


@dataclass
class PRResult:
    pr_number: int
    pr_url: str
    was_existing: bool


class PRClient:
    """
    Opens remediation pull requests on GitHub.

    Idempotent: calling open_remediation_pr() for a branch that already has an open PR
    returns the existing PR rather than creating a duplicate.
    """

    def __init__(self, repo_full_name: str, github_pat: Optional[str] = None):
        self._repo_full_name = repo_full_name
        pat = github_pat or os.environ["GITHUB_PAT"]
        self._gh = Github(pat)
        self._repo = self._gh.get_repo(repo_full_name)

    def open_remediation_pr(
        self,
        branch_name: str,
        base_branch: str,
        change_summary,  # ChangeSummary from code_fixer.py
    ) -> PRResult:
        """
        Open a pull request for `branch_name` against `base_branch`.

        If an open PR for this branch already exists, returns it unchanged (no duplicate).
        Returns a PRResult with pr_number, pr_url, and was_existing flag.
        """
        existing = self._find_open_pr(branch_name, base_branch)
        if existing:
            logger.info(
                "Open PR #%d already exists for branch '%s' — skipping creation.",
                existing.number,
                branch_name,
            )
            return PRResult(
                pr_number=existing.number,
                pr_url=existing.html_url,
                was_existing=True,
            )

        title = (
            f"fix: upgrade {change_summary.component_name} "
            f"from {change_summary.old_version} to {change_summary.new_version} "
            "(vulnerability remediation)"
        )
        body = self._build_pr_body(change_summary)

        try:
            pr = self._repo.create_pull(
                title=title,
                body=body,
                head=branch_name,
                base=base_branch,
                draft=False,
            )
        except GithubException as exc:
            raise RuntimeError(
                f"Failed to create PR for branch '{branch_name}': {exc.data}"
            ) from exc

        logger.info("Created PR #%d: %s", pr.number, pr.html_url)
        return PRResult(pr_number=pr.number, pr_url=pr.html_url, was_existing=False)

    # ── Private helpers ───────────────────────────────────────────────────────

    def _find_open_pr(self, branch_name: str, base_branch: str):
        """Return the first open PR for head=branch_name, or None if none exists."""
        try:
            pulls = self._repo.get_pulls(
                state="open",
                head=f"{self._repo.owner.login}:{branch_name}",
                base=base_branch,
            )
            for pr in pulls:
                return pr
        except GithubException as exc:
            logger.warning("Error checking for existing PRs: %s", exc)
        return None

    def _build_pr_body(self, change_summary) -> str:
        files_list = "\n".join(f"- `{f}`" for f in change_summary.files_changed)
        cve_list = (
            ", ".join(change_summary.cve_ids) if change_summary.cve_ids else "see Nexus IQ report"
        )
        verification_section = self._build_verification_section(change_summary)

        return f"""## OSS Vulnerability Remediation

**Component:** `{change_summary.component_name}`
**Previous version:** `{change_summary.old_version}`
**Remediated version:** `{change_summary.new_version}`
**CVEs addressed:** {cve_list}

---

### Changes made

{files_list}

### Rationale

{change_summary.rationale}
{verification_section}
---

> This PR was opened automatically by the OSS Remediation Agent.
> A Watcher agent is monitoring CI and will attempt up to {change_summary.max_retries} fix cycles
> if CI fails due to the upgrade. If the retry limit is reached, a comment will be added here.
> Human review is required before merge.
"""

    def _build_verification_section(self, change_summary) -> str:
        """
        Surfaces the detected build framework and in-loop unit test outcome so a human
        reviewer sees this without digging into the tracking store. Omitted entirely if
        no framework was detected (degraded mode — CI is the sole verification).
        """
        framework = getattr(change_summary, "framework_detected", None)
        if not framework:
            return "\n"

        test_status = getattr(change_summary, "unit_test_status", None)
        status_lines = {
            "SUCCESS": "All unit tests passed in-loop before this PR was opened.",
            "NO_TESTS_FOUND": "No unit tests were found for this repository.",
            "SOFT_FAIL": (
                "⚠️ Unit tests were still failing after the agent's last fix attempt. "
                "This PR was opened anyway (soft gate) — the customer CI pipeline and the "
                "Watcher's retry loop will catch this. Review the failing tests before merge."
            ),
        }
        test_line = status_lines.get(test_status, "Unit test status was not recorded.")

        return f"""
### Verification

**Build framework:** `{framework}`
**Unit tests:** {test_line}
"""

    def add_comment(self, pr_number: int, comment: str) -> None:
        """Add a comment to a PR (used by the Watcher agent for escalation notices)."""
        pr = self._repo.get_pull(pr_number)
        pr.create_issue_comment(comment)
        logger.info("Added comment to PR #%d", pr_number)

    def open_triage_issue(
        self,
        finding,           # VulnerabilityFinding from nexus_client
        bucket: int,
        rationale: str,
        kb_entry=None,     # Optional KnowledgeEntry
    ) -> int:
        """
        Open a GitHub Issue for a finding the agent cannot automatically fix (bucket 1 or 4).
        Idempotent: returns the existing issue number if one already exists for this component.
        """
        title = (
            f"[OSS Remediation] Manual triage: {finding.component_name} "
            f"{finding.current_version} → {finding.recommended_version}"
        )

        try:
            for issue in self._repo.get_issues(state="open", labels=["oss-remediation-triage"]):
                if issue.title == title:
                    logger.info(
                        "Triage issue already exists (#%d) for %s — skipping.",
                        issue.number, finding.component_name,
                    )
                    return issue.number
        except GithubException as exc:
            logger.warning("Could not search existing issues: %s", exc)

        bucket_labels = {
            1: "Bucket 1 — No fix path available",
            4: "Bucket 4 — Complex framework, major upgrade, no migration KB",
        }
        cve_list = ", ".join(finding.cve_ids) if finding.cve_ids else "see Nexus IQ report"

        kb_section = ""
        if kb_entry and (kb_entry.breaking_changes or kb_entry.migration_steps):
            bc    = "\n".join(f"- {c}" for c in kb_entry.breaking_changes)
            steps = "\n".join(f"{i}. {s}" for i, s in enumerate(kb_entry.migration_steps, 1))
            kb_section = f"""
### Knowledge Base analysis ({kb_entry.source})

**Breaking changes:**
{bc or "_(none documented)_"}

**Suggested migration steps:**
{steps or "_(none documented)_"}
"""

        severity = getattr(finding, "severity", "unknown").upper()
        body = f"""## OSS Remediation — Manual Triage Required

**Component:** `{finding.component_name}`
**Vulnerable version:** `{finding.current_version}`
**Recommended version:** `{finding.recommended_version}`
**Severity:** {severity}
**CVEs:** {cve_list}

### Why the agent skipped this

**{bucket_labels.get(bucket, f'Bucket {bucket}')}**

{rationale}
{kb_section}
### Next steps

1. Review the CVEs linked above and confirm the recommended version is correct.
2. Assess the migration complexity manually.
3. Apply the fix in a dedicated branch and verify CI passes.
4. Close this issue once the PR is merged.

---
> Opened automatically by the OSS Remediation Agent (bucket {bucket}).
> Assign to the relevant team for prioritisation.
"""
        try:
            self._ensure_label(
                "oss-remediation-triage",
                "e11d48",
                "Auto-triage issue from OSS Remediation Agent",
            )
            issue = self._repo.create_issue(
                title=title,
                body=body,
                labels=["oss-remediation-triage"],
            )
            logger.info(
                "Opened triage issue #%d for %s (bucket %d).",
                issue.number, finding.component_name, bucket,
            )
            return issue.number
        except GithubException as exc:
            logger.error(
                "Failed to create triage issue for %s: %s", finding.component_name, exc
            )
            return -1

    def _ensure_label(self, name: str, color: str, description: str) -> None:
        try:
            self._repo.get_label(name)
        except GithubException:
            try:
                self._repo.create_label(name=name, color=color, description=description)
            except GithubException:
                pass  # label creation is best-effort
