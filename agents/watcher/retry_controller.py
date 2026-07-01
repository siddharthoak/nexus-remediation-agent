"""
WAT-02: Retry controller — bounded CI-failure diagnosis and fix loop.

SAFETY-CRITICAL MODULE. The retry bound is a hard safety property, not a configurable
default. Read the safety constraints at the bottom of this docstring before modifying.

Control flow:
  1. Receive a CIResult indicating failure with log text.
  2. Call Claude to diagnose the failure and propose a minimal fix.
  3. Apply the fix to the SAME existing branch (never a new branch or PR).
  4. Push the fix — GitHub Actions re-triggers on push automatically.
  5. Increment the attempt counter for this PR.
  6. If attempts >= MAX_RETRY_ATTEMPTS: stop, post an escalation comment, do not retry.

Safety constraints (non-negotiable):
  - NEVER attempts to merge the PR.
  - NEVER force-pushes (which could discard a human reviewer's own commits to the branch).
  - NEVER exceeds MAX_RETRY_ATTEMPTS even if called again after the limit is reached.
  - The attempt counter backend is injected, not hardcoded, to allow swapping in-memory
    storage for persistent storage (Azure Table Storage / Cosmos DB) in production.
"""

import json
import logging
import os
import re
from typing import Optional, Protocol

import anthropic

logger = logging.getLogger(__name__)


# ── Attempt counter abstraction ───────────────────────────────────────────────
# Kept as a separate injectable component so the POC in-memory counter can be replaced
# with persistent storage (e.g. Azure Table Storage) without changing this module.

class AttemptCounter(Protocol):
    def get(self, pr_number: int) -> int: ...
    def increment(self, pr_number: int) -> int: ...


class InMemoryAttemptCounter:
    """POC-grade in-memory counter. Replace with a persistent backend for production."""

    def __init__(self):
        self._counts: dict = {}

    def get(self, pr_number: int) -> int:
        return self._counts.get(pr_number, 0)

    def increment(self, pr_number: int) -> int:
        self._counts[pr_number] = self._counts.get(pr_number, 0) + 1
        return self._counts[pr_number]


# ── Prompt template ───────────────────────────────────────────────────────────
# Kept as a module-level string for reviewability (same pattern as code_fixer.py).

CI_DIAGNOSIS_PROMPT = """\
You are a Java CI failure analyst. A GitHub Actions CI run has failed on a pull request
that upgrades a dependency. Your job is to diagnose the root cause and propose a MINIMAL fix.

## CI failure log
```
{failure_log}
```

## Current state of relevant files
{file_context}

## Your task
1. Determine whether this failure is CAUSED BY the dependency upgrade (compilation error,
   removed API, changed method signature, config property rename, etc.) or is
   UNRELATED / FLAKY (network timeout, pre-existing test failure, infrastructure issue).
2. If caused by the upgrade, propose a specific, minimal code fix.

Respond in the following JSON format ONLY (no prose before or after):

```json
{{
  "caused_by_upgrade": true,
  "diagnosis": "<one paragraph explaining the root cause>",
  "files_to_change": [
    {{
      "relative_path": "src/main/java/com/example/Foo.java",
      "change_description": "<what specifically needs to change and why>",
      "find": "<exact string to find in this file>",
      "replace": "<exact replacement string>"
    }}
  ]
}}
```

If the failure is NOT caused by the upgrade (flaky test, unrelated issue):
```json
{{
  "caused_by_upgrade": false,
  "diagnosis": "<explanation of why this is unrelated to the dependency upgrade>",
  "files_to_change": []
}}
```
"""


# ── Retry controller ──────────────────────────────────────────────────────────

class RetryLimitExceededError(Exception):
    """Raised when the retry limit for a PR has already been reached."""


class RetryController:
    """
    Diagnoses CI failures and applies fixes to the same PR branch, up to MAX_RETRY_ATTEMPTS.

    See module docstring for safety constraints.
    """

    def __init__(
        self,
        repo_path: str,
        pr_client,                          # pr_client.PRClient instance
        repo_ops,                           # repo_ops.RepoOps instance (already on the right branch)
        model_deployment_name: Optional[str] = None,
        max_retry_attempts: Optional[int] = None,
        attempt_counter: Optional[AttemptCounter] = None,
    ):
        self._repo_path = repo_path
        self._pr_client = pr_client
        self._repo_ops = repo_ops
        self._model = model_deployment_name or os.environ["MODEL_DEPLOYMENT_NAME"]
        # MAX_RETRY_ATTEMPTS is a HARD SAFETY BOUND — not a hint.
        # It defaults to the env var set in the agent YAML, or 3 if not set.
        self._max_attempts = max_retry_attempts or int(os.environ.get("MAX_RETRY_ATTEMPTS", "3"))
        self._counter = attempt_counter or InMemoryAttemptCounter()
        self._client = anthropic.Anthropic()

    def attempt_fix(self, ci_result, branch_name: str) -> bool:
        """
        Diagnose `ci_result`, apply a fix, and push to `branch_name`.

        Returns True if a fix was applied and pushed, False if the failure was classified
        as unrelated/flaky (no fix applied).

        Raises RetryLimitExceededError if this PR has already hit MAX_RETRY_ATTEMPTS.
        The caller must NOT catch this exception to retry anyway — doing so would violate
        the safety constraint.
        """
        pr_number = ci_result.pr_number

        # ── SAFETY CHECK: enforce retry bound before doing anything else ──────
        current_attempts = self._counter.get(pr_number)
        if current_attempts >= self._max_attempts:
            # Limit already reached on a prior call — refuse to retry.
            logger.error(
                "PR #%d has already reached MAX_RETRY_ATTEMPTS=%d. "
                "Refusing to retry. Human review required.",
                pr_number, self._max_attempts,
            )
            raise RetryLimitExceededError(
                f"PR #{pr_number} retry limit ({self._max_attempts}) already reached."
            )

        logger.info(
            "PR #%d: attempt %d/%d — diagnosing CI failure...",
            pr_number, current_attempts + 1, self._max_attempts,
        )

        # Step 1: Call model to diagnose the failure
        diagnosis = self._diagnose_failure(ci_result.failure_log_text)

        if not diagnosis.get("caused_by_upgrade", False):
            logger.warning(
                "PR #%d: CI failure classified as NOT caused by the upgrade. "
                "Diagnosis: %s. Not retrying.",
                pr_number, diagnosis.get("diagnosis", ""),
            )
            self._escalate_unrelated_failure(pr_number, diagnosis)
            return False

        # Step 2: Apply the proposed fix
        files_changed = self._apply_diagnosis_changes(diagnosis)

        if not files_changed:
            logger.warning(
                "PR #%d: model diagnosed an upgrade-caused failure but proposed no file changes. "
                "Escalating.", pr_number,
            )
            self._escalate_no_changes(pr_number, diagnosis)
            return False

        # Step 3: Increment attempt counter BEFORE pushing
        # (so a push failure doesn't leave the counter un-incremented on retry)
        new_count = self._counter.increment(pr_number)

        # Step 4: Commit and push to the SAME branch (never a new branch, never force-push)
        commit_msg = (
            f"fix(ci): retry attempt {new_count}/{self._max_attempts} — "
            f"{diagnosis.get('diagnosis', 'CI failure fix')[:120]}"
        )
        self._repo_ops.commit_changes(commit_msg, files=files_changed)
        self._repo_ops.push_branch(branch_name)
        # GitHub Actions re-triggers automatically on the new push — no explicit API call needed.

        logger.info(
            "PR #%d: pushed fix attempt %d/%d to branch '%s'.",
            pr_number, new_count, self._max_attempts, branch_name,
        )

        # Step 5: Check if we've now hit the limit — if so, proactively escalate
        if new_count >= self._max_attempts:
            logger.warning(
                "PR #%d: reached MAX_RETRY_ATTEMPTS=%d after this push. "
                "Next CI outcome will determine if human review is needed.",
                pr_number, self._max_attempts,
            )
            self._escalate_limit_approaching(pr_number, new_count)

        return True

    # ── Private helpers ───────────────────────────────────────────────────────

    def _diagnose_failure(self, failure_log: str) -> dict:
        """Call Claude to diagnose the CI failure. Returns the parsed JSON response."""
        file_context = self._build_file_context()

        prompt = CI_DIAGNOSIS_PROMPT.format(
            failure_log=failure_log[:8000],  # Truncate very long logs to avoid token limits
            file_context=file_context,
        )

        response = self._client.messages.create(
            model=self._model,
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )

        raw_text = response.content[0].text if response.content else ""
        json_match = re.search(r"```json\s*(.*?)\s*```", raw_text, re.DOTALL)
        json_str = json_match.group(1) if json_match else raw_text.strip()

        try:
            return json.loads(json_str)
        except json.JSONDecodeError:
            # If model response is unparseable, treat as non-upgrade failure to be safe
            logger.error(
                "Could not parse model diagnosis response as JSON. "
                "Treating as unrelated failure to avoid an unsafe retry.\nRaw: %s", raw_text
            )
            return {"caused_by_upgrade": False, "diagnosis": "Unparseable model response", "files_to_change": []}

    def _apply_diagnosis_changes(self, diagnosis: dict) -> list:
        """
        Apply the file changes proposed by the model.
        Returns the list of relative paths that were actually modified.
        Only modifies files explicitly listed in the diagnosis — no other files are touched.
        """
        from pathlib import Path
        files_changed = []
        for file_change in diagnosis.get("files_to_change", []):
            relative_path = file_change.get("relative_path", "")
            find_str = file_change.get("find", "")
            replace_str = file_change.get("replace", "")

            if not relative_path or not find_str:
                logger.warning("Skipping incomplete change spec: %r", file_change)
                continue

            target = Path(self._repo_path) / relative_path
            if not target.exists():
                logger.warning("File not found, skipping: %s", relative_path)
                continue

            content = target.read_text(encoding="utf-8")
            if find_str not in content:
                logger.warning("Find string not found in %s — skipping.", relative_path)
                continue

            target.write_text(content.replace(find_str, replace_str, 1), encoding="utf-8")
            files_changed.append(relative_path)
            logger.info("Applied CI fix to %s", relative_path)

        return files_changed

    def _escalate_unrelated_failure(self, pr_number: int, diagnosis: dict) -> None:
        """Post a PR comment when CI failure is unrelated to the upgrade."""
        comment = (
            "## OSS Remediation Agent — CI Failure (Unrelated)\n\n"
            "The CI failure on this PR has been classified as **not caused by the dependency "
            "upgrade**. No automatic fix will be attempted.\n\n"
            f"**Diagnosis:** {diagnosis.get('diagnosis', 'See CI logs for details.')}\n\n"
            "Please investigate the CI failure manually and resolve it, or re-run the failed "
            "check if it appears to be a transient/flaky failure."
        )
        self._post_escalation_comment(pr_number, comment)

    def _escalate_no_changes(self, pr_number: int, diagnosis: dict) -> None:
        """Post a PR comment when the model diagnoses a problem but can't propose a fix."""
        comment = (
            "## OSS Remediation Agent — Fix Needed\n\n"
            "The CI failure appears to be caused by the dependency upgrade, but the agent "
            "could not determine the required code changes automatically.\n\n"
            f"**Diagnosis:** {diagnosis.get('diagnosis', 'See CI logs.')}\n\n"
            "Human review and a manual fix are needed before this PR can pass CI."
        )
        self._post_escalation_comment(pr_number, comment)

    def _escalate_limit_approaching(self, pr_number: int, attempt_count: int) -> None:
        """Post a PR comment warning that the retry limit has been reached."""
        comment = (
            f"## OSS Remediation Agent — Retry Limit Reached ({attempt_count}/{self._max_attempts})\n\n"
            "This PR has consumed all automatic retry attempts. If CI fails again, **no further "
            "automatic fixes will be applied**. Human review is required.\n\n"
            "Please inspect the CI logs and apply any remaining fixes manually."
        )
        self._post_escalation_comment(pr_number, comment)

    def _post_escalation_comment(self, pr_number: int, comment: str) -> None:
        try:
            self._pr_client.add_comment(pr_number, comment)
        except Exception as exc:
            # Never let a failed comment prevent the caller from knowing the retry was done
            logger.error("Could not post escalation comment on PR #%d: %s", pr_number, exc)

    def _build_file_context(self) -> str:
        """Provide recently-changed Java/config files as context for the model."""
        from pathlib import Path
        files = []
        for ext in ("*.java", "*.xml", "*.properties"):
            for f in Path(self._repo_path).rglob(ext):
                if "target" not in f.parts:
                    files.append(str(f.relative_to(self._repo_path)))
        return "\n".join(sorted(files)[:100])
