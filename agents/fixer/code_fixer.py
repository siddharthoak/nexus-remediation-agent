"""
FIX-03 (refactored): Code fixer — the ONLY component that writes code or pushes commits.

Two public entry points:
  run_fresh_fix(finding, tracking_id, tracking_store, repo_path, repo_ops)
      Called by main.py when the scheduler triggers a fresh Nexus scan.

  run_retry_fix(tracking_id, tracking_store, repo_path, repo_ops)
      Called by main.py when RETRY_TRACKING_ID env var is set (Watcher-triggered).
      Validates the tracking record before acting — refuses if status is not
      RETRY_REQUESTED or if attempt_number exceeds MAX_RETRY_ATTEMPTS.

Both entry points funnel into the same _execute_fix() function. The only difference
is what context is passed to the model: a fresh run has no failure history; a retry
passes the failure_log_excerpt from the tracking record so the model does not re-attempt
the same fix blind.

Model interaction uses a tool-use loop: Claude calls read_file and grep_files to inspect
real source code in the cloned repo before writing find/replace strings. This replaces the
earlier approach of passing only file paths and relying on training knowledge of library APIs.

Invariant: this module never reads RETRY_TRACKING_ID from env vars directly.
The caller (main.py) resolves which path to take and passes the tracking_id explicitly.
"""

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import anthropic

from common.tracking_store import TrackingStatus
from common.telemetry import emit_event
from frameworks import detect_framework

logger = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 10  # Guard against runaway agentic loops

BUILD_OUTPUT_CAP = 10_000   # chars; truncated from the end — first errors are most useful
TEST_OUTPUT_CAP = 20_000    # chars; test failure output can be much larger than compiler output


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class ChangeSummary:
    component_name: str
    old_version: str
    new_version: str
    files_changed: list = field(default_factory=list)
    rationale: str = ""
    cve_ids: list = field(default_factory=list)
    max_retries: int = 3
    prompt_tokens: int = 0
    completion_tokens: int = 0
    framework_detected: Optional[str] = None
    unit_test_status: Optional[str] = None  # "SUCCESS" | "NO_TESTS_FOUND" | "SOFT_FAIL" | None
    kb_hit: bool = False  # True if stored KB patterns were applied with no model call


# ── Tool definitions ──────────────────────────────────────────────────────────
# Passed to the Anthropic API so Claude can inspect the real repo before proposing changes.

TOOLS = [
    {
        "name": "grep_files",
        "description": (
            "Search for a regex pattern across repository source files. "
            "Returns matching file paths with line numbers and excerpts. "
            "Use this first to find files that import or reference the dependency being upgraded."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Regex or literal string to search for across file contents.",
                },
                "extensions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "File extensions to include, e.g. [\".java\", \".xml\"]. "
                        "Defaults to .java, .kt, .groovy, .xml, .properties, .yml, .yaml, "
                        ".ts, .tsx, .js, .jsx, .json, .py."
                    ),
                },
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "read_file",
        "description": (
            "Read the full contents of a file in the cloned repository. "
            "Use this to inspect actual source code before writing any find/replace strings. "
            "Never pass a 'find' value to apply_file_change for a file you have not read."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "relative_path": {
                    "type": "string",
                    "description": (
                        "Path relative to the repository root, "
                        "e.g. src/main/java/com/example/Foo.java"
                    ),
                }
            },
            "required": ["relative_path"],
        },
    },
    {
        "name": "apply_file_change",
        "description": (
            "Apply a single find→replace edit to a file in the cloned repository. "
            "The 'find' string MUST be an exact substring of the file as returned by read_file — "
            "never guess or paraphrase it. "
            "Changes are written to disk immediately and can be verified with run_build. "
            "Do NOT edit the dependency manifest (pom.xml, build.gradle, package.json, "
            "requirements.txt, pyproject.toml) — the version bump is already applied separately."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "relative_path": {
                    "type": "string",
                    "description": "Path relative to the repository root.",
                },
                "find": {
                    "type": "string",
                    "description": "Exact string to replace — must match read_file output verbatim.",
                },
                "replace": {
                    "type": "string",
                    "description": "Replacement string.",
                },
                "change_description": {
                    "type": "string",
                    "description": "Brief explanation of why this change is required by the upgrade.",
                },
            },
            "required": ["relative_path", "find", "replace"],
        },
    },
    {
        "name": "run_build",
        "description": (
            "Compile/typecheck the repository using its detected build framework "
            "(Maven, Gradle, npm, or Python). Call this after applying file changes to verify "
            "the change compiles cleanly. If it fails, read the error, inspect the affected "
            "files with read_file, apply corrections with apply_file_change, and build again. "
            "No tests are executed. Always call this before run_unit_tests."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "run_unit_tests",
        "description": (
            "Run the unit test suite using the detected build framework, once run_build has "
            "succeeded. Integration tests are structurally excluded — only unit tests run. "
            "If tests fail, read the failure output, apply corrections, re-run run_build, "
            "then run_unit_tests again. This is a soft gate: if tests are still failing when "
            "you are out of tool rounds, end_turn anyway — the failure will be recorded and "
            "surfaced in the PR for human review."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
]


# ── Prompt templates ──────────────────────────────────────────────────────────

FRESH_FIX_PROMPT = """\
You are a software dependency upgrade specialist. Apply the MINIMAL set of code
changes required to upgrade a specific dependency from one version to another.

## Detected build framework
{framework_name}

## Dependency being upgraded
- Component: {component_name}
- Current version: {current_version}
- Target version: {target_version}
{kb_context}
## Repository file tree (paths only)
{file_listing}

## Available tools
- `grep_files(pattern, extensions?)` — regex search across file contents.
- `read_file(relative_path)` — read a file's full content.
- `apply_file_change(relative_path, find, replace, change_description?)` — write a find→replace edit to disk immediately.
- `run_build()` — compile/typecheck using the detected build framework. No tests. Returns build error output on failure.
- `run_unit_tests()` — run unit tests using the detected build framework. Integration tests are excluded.

## Your workflow
1. Call grep_files with the import/package pattern for {component_name}
   (e.g. for "org.apache.logging.log4j:log4j-core", search "org\\.apache\\.logging\\.log4j").
2. Call read_file on each affected file to inspect the actual source code.
3. Identify which API/behavioral changes between {current_version} and {target_version}
   require source-level changes (removed/renamed methods, config format changes).
4. Call apply_file_change for each required edit.
   The "find" value MUST be an exact substring of the file content from read_file — never guess.
   Do NOT edit the dependency manifest — the version bump is already applied.
5. Call run_build to verify the changes compile cleanly.
6. If the build fails: read the error, inspect the affected files, apply corrections, build again.
7. Once the build succeeds, call run_unit_tests. If tests fail, read the output, apply
   corrections, re-run run_build, then run_unit_tests again.
8. When the build succeeds (or if no source changes are needed), return end_turn with JSON —
   even if unit tests are still failing after your last attempt.

## CRITICAL CONSTRAINTS
- Only apply changes strictly required by the version upgrade.
- Do NOT refactor, rename, reformat, or improve unrelated code.
- Never pass a "find" value you have not verified verbatim in read_file output.

```json
{{
  "rationale": "<key API changes between versions and summary of what was changed>"
}}
```
"""

RETRY_FIX_PROMPT = """\
You are a software dependency upgrade specialist. A previous fix attempt for this
dependency upgrade FAILED CI. Diagnose the CI failure and apply a corrective fix.

## Detected build framework
{framework_name}

## Dependency being upgraded
- Component: {component_name}
- Current version: {current_version}
- Target version: {target_version}

## Previous CI failure log (root cause of the failure)
```
{failure_log_excerpt}
```
{kb_context}
## Repository file tree (paths only)
{file_listing}

## Available tools
- `grep_files(pattern, extensions?)` — regex search across file contents.
- `read_file(relative_path)` — read a file's full content.
- `apply_file_change(relative_path, find, replace, change_description?)` — write a find→replace edit to disk immediately.
- `run_build()` — compile/typecheck using the detected build framework. No tests. Returns build error output on failure.
- `run_unit_tests()` — run unit tests using the detected build framework. Integration tests are excluded.

## Your workflow
1. Analyse the CI failure log to identify the ROOT CAUSE.
2. Use grep_files and read_file to inspect the files mentioned in the failure log.
3. Call apply_file_change for the specific, minimal change that fixes the CI failure.
   Do NOT repeat the same change from the previous attempt unless the log shows it was incomplete.
4. Call run_build to verify the fix compiles cleanly.
5. If the build fails: read the error, inspect affected files, apply corrections, build again.
6. Once the build succeeds, call run_unit_tests to confirm the fix didn't break unit tests.
7. When the build succeeds, return end_turn with JSON — even if unit tests are still
   failing after your last attempt.

## CRITICAL CONSTRAINTS
- Fix only what the CI failure log tells you is broken.
- Do NOT refactor, rename, reformat, or improve unrelated code.
- Do NOT edit the dependency manifest.
- Never pass a "find" value you have not verified verbatim in read_file output.

```json
{{
  "rationale": "<diagnosis of the CI failure and summary of what was changed>"
}}
```
"""


def _build_kb_context(kb_entry) -> str:
    """
    Render a KnowledgeEntry (Tier 1 learned / Tier 2 playbook / Knowledge Agent) as a
    prompt section. Returns "" if kb_entry is None so the prompt templates' {kb_context}
    placeholder collapses to a blank line rather than leaving a stray heading.
    """
    if kb_entry is None:
        return ""

    lines = [f"\n## Knowledge base context (source: {kb_entry.source}, confidence: {kb_entry.confidence})"]
    if kb_entry.breaking_changes:
        lines.append("Known breaking changes:")
        lines += [f"- {c}" for c in kb_entry.breaking_changes]
    if kb_entry.api_removals:
        lines.append("Removed/renamed APIs:")
        lines += [f"- {a}" for a in kb_entry.api_removals]
    if kb_entry.migration_steps:
        lines.append("Suggested migration steps:")
        lines += [f"{i}. {s}" for i, s in enumerate(kb_entry.migration_steps, 1)]
    if kb_entry.patterns:
        lines.append(
            "Previously seen fix patterns for this exact upgrade (already attempted "
            "automatically before this loop started — verify against read_file output "
            "before reapplying, since they may not match this repo's exact content):"
        )
        for p in kb_entry.patterns:
            lines.append(f"- find: {p.get('find', '')!r} -> replace: {p.get('replace', '')!r}"
                         f" ({p.get('description', '')})")
    return "\n".join(lines) + "\n"


# ── Exceptions ────────────────────────────────────────────────────────────────

class CodeFixerError(Exception):
    """Raised when the model response cannot be parsed into the expected format."""


class InvalidRetryError(Exception):
    """
    Raised when run_retry_fix() is called with a tracking_id that fails validation.
    This is the enforcement point preventing the Fixer from being self-invoked or
    called by anything other than a Watcher-gated RETRY_REQUESTED record.
    """


# ── CodeFixer ─────────────────────────────────────────────────────────────────

class CodeFixer:
    """
    Applies dependency upgrade fixes to a cloned repository.

    This class owns all model calls and all file mutations. The Watcher never
    instantiates or calls this class.

    Model interaction uses a tool-use loop: Claude calls grep_files and read_file
    to inspect real source before proposing find/replace changes, making the
    'find' strings exact matches rather than training-knowledge guesses.
    """

    def __init__(
        self,
        repo_path: str,
        model_deployment_name: Optional[str] = None,
        kb_store=None,
    ):
        self._repo_path = Path(repo_path)
        self._model = model_deployment_name or os.environ["MODEL_DEPLOYMENT_NAME"]
        self._client = anthropic.Anthropic()
        self._max_attempts = int(os.environ.get("MAX_RETRY_ATTEMPTS", "3"))
        self._applied_changes: list[str] = []  # paths written by apply_file_change during the loop
        self._framework = detect_framework(self._repo_path)  # None in degraded mode
        self._last_test_status: Optional[str] = None  # last run_unit_tests() result this loop
        self._kb_store = kb_store  # optional KnowledgeStoreProtocol; None disables the KB-hit fast path

    # ── Public entry points ───────────────────────────────────────────────────

    def run_fresh_fix(
        self,
        component_name: str,
        current_version: str,
        target_version: str,
        tracking_id: str,
        tracking_store,
        cve_ids: Optional[list] = None,
    ) -> ChangeSummary:
        """
        Entry point (a): scheduled fresh-scan fix.
        Updates the tracking record from CREATED through to completion.
        """
        logger.info(
            "[fresh] %s: %s → %s (tracking=%s)",
            component_name, current_version, target_version, tracking_id[:8],
        )

        record = tracking_store.get(tracking_id)
        if record is None:
            raise ValueError(f"Tracking record {tracking_id} not found.")

        summary = self._execute_fix(
            component_name=component_name,
            current_version=current_version,
            target_version=target_version,
            cve_ids=cve_ids or [],
            failure_log_excerpt=None,
        )

        record.token_usage = {
            "prompt_tokens": summary.prompt_tokens,
            "completion_tokens": summary.completion_tokens,
        }
        record.framework_detected = summary.framework_detected
        record.unit_test_status = summary.unit_test_status
        record.kb_hit = summary.kb_hit
        tracking_store.update(record)
        emit_event(
            "FixAttemptCompleted",
            mode="fresh",
            tracking_id=tracking_id,
            repo=record.repo,
            component_name=component_name,
            old_version=current_version,
            new_version=target_version,
            cve_ids=cve_ids,
            attempt_number=record.attempt_number,
            framework_detected=summary.framework_detected,
            unit_test_status=summary.unit_test_status,
            kb_hit=summary.kb_hit,
            prompt_tokens=summary.prompt_tokens,
            completion_tokens=summary.completion_tokens,
            total_tokens=summary.prompt_tokens + summary.completion_tokens,
        )
        return summary

    def run_retry_fix(
        self,
        tracking_id: str,
        tracking_store,
    ) -> ChangeSummary:
        """
        Entry point (b): Watcher-triggered retry fix.

        Validates the tracking record before acting:
          - status must be RETRY_REQUESTED (only the Watcher sets this)
          - attempt_number must be <= MAX_RETRY_ATTEMPTS

        Raises InvalidRetryError if either check fails.
        """
        record = tracking_store.get(tracking_id)

        if record is None:
            raise InvalidRetryError(
                f"Tracking record {tracking_id} not found. "
                "Cannot retry a fix without a valid Watcher-issued tracking record."
            )

        if record.status != TrackingStatus.RETRY_REQUESTED.value:
            raise InvalidRetryError(
                f"Tracking record {tracking_id} has status={record.status!r}, "
                f"expected {TrackingStatus.RETRY_REQUESTED.value!r}. "
                "The Fixer's retry entry point may only be invoked by the Watcher "
                "through a RETRY_REQUESTED record. Refusing to act."
            )

        if record.attempt_number > self._max_attempts:
            raise InvalidRetryError(
                f"Tracking record {tracking_id} has attempt_number={record.attempt_number} "
                f"which exceeds MAX_RETRY_ATTEMPTS={self._max_attempts}. "
                "Retry limit already exhausted. Refusing to act."
            )

        logger.info(
            "[retry] %s: %s → %s attempt %d/%d (tracking=%s)",
            record.component_name, record.old_version, record.new_version,
            record.attempt_number, self._max_attempts, tracking_id[:8],
        )

        if not record.failure_log_excerpt:
            logger.warning(
                "Retry tracking record %s has no failure_log_excerpt — "
                "proceeding with reduced context (Watcher should always set this).",
                tracking_id[:8],
            )

        summary = self._execute_fix(
            component_name=record.component_name,
            current_version=record.old_version,
            target_version=record.new_version,
            cve_ids=[record.vulnerability_id] if record.vulnerability_id else [],
            failure_log_excerpt=record.failure_log_excerpt,
        )

        record.token_usage = {
            "prompt_tokens": summary.prompt_tokens,
            "completion_tokens": summary.completion_tokens,
        }
        record.framework_detected = summary.framework_detected
        record.unit_test_status = summary.unit_test_status
        record.kb_hit = summary.kb_hit
        tracking_store.update(record)
        emit_event(
            "FixAttemptCompleted",
            mode="retry",
            tracking_id=tracking_id,
            repo=record.repo,
            component_name=record.component_name,
            old_version=record.old_version,
            new_version=record.new_version,
            cve_ids=[record.vulnerability_id] if record.vulnerability_id else [],
            pr_number=record.pr_number,
            attempt_number=record.attempt_number,
            framework_detected=summary.framework_detected,
            unit_test_status=summary.unit_test_status,
            kb_hit=summary.kb_hit,
            prompt_tokens=summary.prompt_tokens,
            completion_tokens=summary.completion_tokens,
            total_tokens=summary.prompt_tokens + summary.completion_tokens,
        )
        return summary

    # ── Core fix logic (shared by both entry points) ──────────────────────────

    def _execute_fix(
        self,
        component_name: str,
        current_version: str,
        target_version: str,
        cve_ids: list,
        failure_log_excerpt: Optional[str],
    ) -> ChangeSummary:
        # Step 1: Bump the dependency manifest via the detected framework — never let the
        # model touch the manifest directly. In degraded mode (no framework detected),
        # there is no manifest to bump; Claude proceeds without a compile/test gate.
        if self._framework is not None:
            self._framework.bump_dependency(
                self._repo_path, component_name, current_version, target_version
            )
        else:
            logger.warning(
                "No supported build framework detected at %s — skipping automatic manifest "
                "bump and compile/test gates. CI is the fallback verification.",
                self._repo_path,
            )

        # Step 2: Check the Knowledge Store for a KB-hit fast path before any model call.
        # Only Tier 1 (learned) / Tier 2 (playbook) / Knowledge Agent entries with concrete
        # find/replace patterns are eligible — an entry with only prose (breaking_changes,
        # migration_steps) can't be mechanically applied and falls through to the model,
        # which still receives it as grounding context (see _call_model's kb_context).
        self._applied_changes = []
        self._last_test_status = None
        kb_entry = None
        if self._kb_store is not None:
            kb_entry = self._kb_store.find_applicable(component_name, current_version, target_version)

        kb_hit = False
        if kb_entry and kb_entry.patterns and self._framework is not None:
            kb_hit = self._try_kb_patterns(kb_entry)

        if kb_hit:
            manifest_files = [self._framework.manifest_file] if self._framework else []
            files_changed = manifest_files + list(dict.fromkeys(self._applied_changes))
            unit_test_status = (
                self._last_test_status if self._last_test_status in ("SUCCESS", "NO_TESTS_FOUND") else None
            )
            return ChangeSummary(
                component_name=component_name,
                old_version=current_version,
                new_version=target_version,
                files_changed=files_changed,
                rationale=(
                    f"Applied {len(kb_entry.patterns)} stored fix pattern(s) from the Knowledge Base "
                    f"({kb_entry.source}, confidence={kb_entry.confidence}) directly — no model call "
                    "was required. Build and unit test gates both passed."
                ),
                cve_ids=cve_ids,
                prompt_tokens=0,
                completion_tokens=0,
                framework_detected=self._framework.name if self._framework else None,
                unit_test_status=unit_test_status,
                kb_hit=True,
            )

        # Step 3: Fall back to the tool-use loop — Claude inspects files, applies changes,
        # verifies build. If a KB-hit attempt was made above and failed (build/test failure,
        # patterns didn't match this repo's content), any changes it already wrote to disk
        # remain — Claude sees them via read_file and self-corrects rather than starting blind.
        # kb_entry (if present) is injected into the prompt as grounded context either way.
        file_listing = self._build_file_listing()
        reasoning, prompt_tokens, completion_tokens = self._call_model(
            component_name=component_name,
            current_version=current_version,
            target_version=target_version,
            file_listing=file_listing,
            failure_log_excerpt=failure_log_excerpt,
            kb_entry=kb_entry,
        )

        # Deduplicate paths while preserving order — a file may have been corrected more than once.
        manifest_files = [self._framework.manifest_file] if self._framework else []
        files_changed = manifest_files + list(dict.fromkeys(self._applied_changes))

        if self._last_test_status in ("SUCCESS", "NO_TESTS_FOUND"):
            unit_test_status = self._last_test_status
        elif self._last_test_status is not None:
            unit_test_status = "SOFT_FAIL"
        else:
            unit_test_status = None

        return ChangeSummary(
            component_name=component_name,
            old_version=current_version,
            new_version=target_version,
            files_changed=files_changed,
            rationale=reasoning.get("rationale", ""),
            cve_ids=cve_ids,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            framework_detected=self._framework.name if self._framework else None,
            unit_test_status=unit_test_status,
            kb_hit=False,
        )

    # ── KB-hit fast path (Tier 1 / Tier 2 / Knowledge Agent patterns, no model call) ──

    def _try_kb_patterns(self, kb_entry) -> bool:
        """
        Attempt to apply every stored find/replace pattern in kb_entry mechanically,
        then run the same build + unit-test gates the model-driven loop would run.
        Returns True only if at least one pattern matched this repo AND both gates
        passed (or NO_TESTS_FOUND) — the caller treats that as a full KB hit and
        skips the Claude call entirely. Returns False otherwise (including "no
        pattern matched anything"), in which case any changes already written to
        disk are left in place for the model loop to inspect and correct.
        """
        applied_total = 0
        for pattern in kb_entry.patterns:
            find_str = pattern.get("find", "")
            replace_str = pattern.get("replace", "")
            if not find_str:
                continue
            applied_total += self._apply_pattern_repo_wide(find_str, replace_str)

        if applied_total == 0:
            logger.info(
                "KB entry (%s) for %s found, but none of its %d pattern(s) matched this "
                "repo's content — falling back to the model.",
                kb_entry.source, kb_entry.component_name, len(kb_entry.patterns),
            )
            return False

        build_result = self._framework.build(self._repo_path)
        if not build_result.success:
            logger.info(
                "KB-hit pattern application for %s failed to build — falling back to the "
                "model with KB context injected.",
                kb_entry.component_name,
            )
            return False

        test_result = self._framework.test_unit(self._repo_path)
        self._last_test_status = test_result.status
        if test_result.status not in ("SUCCESS", "NO_TESTS_FOUND"):
            logger.info(
                "KB-hit pattern application for %s built cleanly but unit tests failed — "
                "falling back to the model with KB context injected.",
                kb_entry.component_name,
            )
            return False

        logger.info(
            "KB hit (%s): %d pattern(s) applied across %d file(s) for %s — build and unit "
            "tests both passed, no model call required.",
            kb_entry.source, len(kb_entry.patterns), applied_total, kb_entry.component_name,
        )
        return True

    def _apply_pattern_repo_wide(self, find_str: str, replace_str: str) -> int:
        """
        Apply a single find->replace pattern across every source file in the repo that
        contains find_str verbatim. Unlike apply_file_change (used by the model loop,
        which targets one file and replaces only the first occurrence for precision),
        a stored KB pattern has no associated file path — it was learned from a
        previous repo's diff — so it must be matched by content, and every occurrence
        in a matching file is replaced since the pattern was vetted as safe/deterministic
        at learning time (see PatternLearner's extraction prompt).
        Returns the number of files modified.
        """
        exts = {
            ".java", ".kt", ".groovy", ".xml", ".properties", ".yml", ".yaml",
            ".ts", ".tsx", ".js", ".jsx", ".json", ".py",
        }
        excluded_dirs = {"target", "build", "node_modules", ".venv", "venv", ".git"}
        modified = 0
        for f in sorted(self._repo_path.rglob("*")):
            if excluded_dirs & set(f.parts) or f.suffix not in exts:
                continue
            try:
                content = f.read_text(encoding="utf-8")
            except Exception:
                continue
            if find_str not in content:
                continue
            f.write_text(content.replace(find_str, replace_str), encoding="utf-8")
            rel = str(f.relative_to(self._repo_path))
            self._applied_changes.append(rel)
            modified += 1
            logger.info("KB pattern applied to %s", rel)
        return modified

    # ── Model call (agentic tool-use loop) ────────────────────────────────────

    def _call_model(
        self,
        component_name: str,
        current_version: str,
        target_version: str,
        file_listing: str,
        failure_log_excerpt: Optional[str],
        kb_entry=None,
    ) -> tuple:
        """
        Agentic tool-use loop. Claude calls read_file / grep_files to inspect real
        source before writing find/replace strings, then ends with the JSON answer.
        Returns (reasoning_dict, total_prompt_tokens, total_completion_tokens).

        Token counts are accumulated across all rounds so the tracking record
        reflects the true cost of the entire conversation, not just the last turn.

        kb_entry, if present, anchors Claude to retrieved/learned ground truth (Knowledge
        Agent web research, a Tier 2 playbook, or a prior Tier 1 confirmed fix) rather than
        parametric training knowledge, which may be stale for recently published versions.
        """
        framework_name = self._framework.name if self._framework else (
            "none detected — no compile/test gate available; CI is the fallback verification"
        )
        kb_context = _build_kb_context(kb_entry)

        if failure_log_excerpt:
            prompt = RETRY_FIX_PROMPT.format(
                framework_name=framework_name,
                component_name=component_name,
                current_version=current_version,
                target_version=target_version,
                failure_log_excerpt=failure_log_excerpt[:6000],
                file_listing=file_listing,
                kb_context=kb_context,
            )
        else:
            prompt = FRESH_FIX_PROMPT.format(
                framework_name=framework_name,
                component_name=component_name,
                current_version=current_version,
                target_version=target_version,
                file_listing=file_listing,
                kb_context=kb_context,
            )

        messages = [{"role": "user", "content": prompt}]
        total_prompt_tokens = 0
        total_completion_tokens = 0

        for round_num in range(MAX_TOOL_ROUNDS):
            response = self._client.messages.create(
                model=self._model,
                max_tokens=4096,
                tools=TOOLS,
                messages=messages,
            )

            total_prompt_tokens     += response.usage.input_tokens  if response.usage else 0
            total_completion_tokens += response.usage.output_tokens if response.usage else 0

            if response.stop_reason == "end_turn":
                raw_text = next(
                    (b.text for b in response.content if hasattr(b, "text")), ""
                )
                json_match = re.search(r"```json\s*(.*?)\s*```", raw_text, re.DOTALL)
                json_str = json_match.group(1) if json_match else raw_text.strip()
                try:
                    return json.loads(json_str), total_prompt_tokens, total_completion_tokens
                except json.JSONDecodeError as exc:
                    raise CodeFixerError(
                        f"Model response could not be parsed as JSON: {exc}\n\nRaw:\n{raw_text}"
                    ) from exc

            if response.stop_reason == "tool_use":
                tool_results = []
                for block in response.content:
                    if block.type == "tool_use":
                        logger.debug(
                            "Tool call [round %d]: %s(%s)",
                            round_num + 1, block.name, block.input,
                        )
                        content = self._dispatch_tool(block.name, block.input)
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": content,
                        })
                messages.append({"role": "assistant", "content": response.content})
                messages.append({"role": "user",       "content": tool_results})
                continue

            raise CodeFixerError(
                f"Unexpected stop_reason={response.stop_reason!r} in round {round_num + 1}."
            )

        raise CodeFixerError(
            f"Model did not produce a final JSON answer within {MAX_TOOL_ROUNDS} tool rounds."
        )

    # ── Tool dispatch ─────────────────────────────────────────────────────────

    def _dispatch_tool(self, name: str, inputs: dict) -> str:
        if name == "read_file":
            return self._tool_read_file(inputs.get("relative_path", ""))
        if name == "grep_files":
            return self._tool_grep_files(
                inputs.get("pattern", ""),
                inputs.get("extensions", []),
            )
        if name == "apply_file_change":
            return self._tool_apply_file_change(
                inputs.get("relative_path", ""),
                inputs.get("find", ""),
                inputs.get("replace", ""),
            )
        if name == "run_build":
            return self._tool_run_build()
        if name == "run_unit_tests":
            return self._tool_run_unit_tests()
        return f"Unknown tool: {name!r}"

    def _tool_read_file(self, relative_path: str) -> str:
        if not relative_path:
            return "ERROR: relative_path is required."
        target = self._repo_path / relative_path
        if not target.exists():
            return f"ERROR: File not found: {relative_path}"
        try:
            content = target.read_text(encoding="utf-8")
            if len(content) > 50_000:
                content = content[:50_000] + "\n... [truncated at 50 000 chars]"
            return content
        except Exception as exc:
            return f"ERROR reading {relative_path}: {exc}"

    def _tool_grep_files(self, pattern: str, extensions: list) -> str:
        if not pattern:
            return "ERROR: pattern is required."
        exts = set(extensions) if extensions else {
            ".java", ".kt", ".groovy", ".xml", ".properties", ".yml", ".yaml",
            ".ts", ".tsx", ".js", ".jsx", ".json", ".py",
        }
        try:
            compiled = re.compile(pattern)
        except re.error as exc:
            return f"ERROR: invalid regex {pattern!r}: {exc}"

        excluded_dirs = {"target", "build", "node_modules", ".venv", "venv", ".git"}
        results = []
        for f in sorted(self._repo_path.rglob("*")):
            if excluded_dirs & set(f.parts) or f.suffix not in exts:
                continue
            try:
                lines = f.read_text(encoding="utf-8", errors="ignore").splitlines()
                matches = [
                    f"  {i + 1}: {line.rstrip()}"
                    for i, line in enumerate(lines)
                    if compiled.search(line)
                ]
                if matches:
                    rel = str(f.relative_to(self._repo_path))
                    results.append(f"{rel}:\n" + "\n".join(matches[:15]))
            except Exception:
                pass

        if not results:
            return "No matches found."
        return "\n\n".join(results[:30])

    # ── File change application ───────────────────────────────────────────────

    def _tool_apply_file_change(self, relative_path: str, find_str: str, replace_str: str) -> str:
        """
        Tool handler: write a single find→replace edit to disk immediately.
        Returns an OK or ERROR string that Claude sees as the tool result.
        An ERROR result prompts Claude to re-read the file before retrying.
        """
        if not relative_path or not find_str:
            return "ERROR: relative_path and find are both required."
        target = self._repo_path / relative_path
        if not target.exists():
            return f"ERROR: File not found: {relative_path}"
        content = target.read_text(encoding="utf-8")
        if find_str not in content:
            return (
                f"ERROR: find string not present in {relative_path}. "
                "It must be an exact substring of the file as returned by read_file. "
                "Call read_file again to check the current file state before retrying."
            )
        target.write_text(content.replace(find_str, replace_str, 1), encoding="utf-8")
        self._applied_changes.append(relative_path)
        logger.info("apply_file_change: modified %s", relative_path)
        return f"OK: change applied to {relative_path}"

    def _tool_run_build(self) -> str:
        """
        Tool handler: delegate to the detected framework's build() (compile/typecheck only).
        Returns a success message or the build output (capped) for Claude to diagnose.
        Degrades to an ERROR string if no framework was detected — Claude proceeds to
        end_turn without a compile gate; CI is the fallback.
        """
        if self._framework is None:
            return "ERROR: no supported build file found"

        result = self._framework.build(self._repo_path)
        if result.success:
            return f"build ({self._framework.name}): SUCCESS — no compilation errors."
        return (
            f"build ({self._framework.name}): FAILED\n\n"
            f"{result.output[:BUILD_OUTPUT_CAP]}"
        )

    def _tool_run_unit_tests(self) -> str:
        """
        Tool handler: delegate to the detected framework's test_unit() (unit tests only,
        integration tests structurally excluded). Records the outcome in
        self._last_test_status so _execute_fix can populate ChangeSummary.unit_test_status
        after the loop ends. This is a soft gate — Claude may end_turn even on failure.
        """
        if self._framework is None:
            return "ERROR: no supported build file found"

        result = self._framework.test_unit(self._repo_path)
        self._last_test_status = result.status

        if result.status == "SUCCESS":
            return f"tests ({self._framework.name}): SUCCESS — all unit tests passed."
        if result.status == "NO_TESTS_FOUND":
            return f"tests ({self._framework.name}): NO_TESTS_FOUND — treat as success."
        return (
            f"tests ({self._framework.name}): {result.status}\n\n"
            f"{result.output[:TEST_OUTPUT_CAP]}"
        )

    # ── File listing (orientation only — content is read via tools) ───────────

    def _build_file_listing(self) -> str:
        files = []
        for ext in (
            "*.java", "*.kt", "*.groovy", "*.xml", "*.properties", "*.yml", "*.yaml",
            "*.ts", "*.tsx", "*.js", "*.jsx", "*.json", "*.py",
        ):
            for f in self._repo_path.rglob(ext):
                if not ({"target", "build", "node_modules", ".venv", "venv", ".git"} & set(f.parts)):
                    files.append(str(f.relative_to(self._repo_path)))
        return "\n".join(sorted(files)[:200])
