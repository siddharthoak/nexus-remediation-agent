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
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
import xml.etree.ElementTree as ET

import anthropic

from common.tracking_store import TrackingStatus

logger = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 10  # Guard against runaway agentic loops


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
                        "Defaults to .java, .xml, .properties, .yml, .yaml."
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
            "Changes are written to disk immediately and can be verified with run_maven_compile. "
            "Do NOT edit pom.xml — the version bump is already applied separately."
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
        "name": "run_maven_compile",
        "description": (
            "Compile the repository with 'mvn compile -q'. "
            "Call this after applying file changes to verify compilation succeeds. "
            "If it fails, read the compiler error, inspect the affected files with read_file, "
            "apply corrections with apply_file_change, and compile again. "
            "No tests are executed — compile only."
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
You are a Java/Maven dependency upgrade specialist. Apply the MINIMAL set of code
changes required to upgrade a specific dependency from one version to another.

## Dependency being upgraded
- Component: {component_name}
- Current version: {current_version}
- Target version: {target_version}

## Repository file tree (paths only)
{file_listing}

## Available tools
- `grep_files(pattern, extensions?)` — regex search across file contents.
- `read_file(relative_path)` — read a file's full content.
- `apply_file_change(relative_path, find, replace, change_description?)` — write a find→replace edit to disk immediately.
- `run_maven_compile()` — run 'mvn compile -q'. No tests. Returns compiler error output on failure.

## Your workflow
1. Call grep_files with the import/package pattern for {component_name}
   (e.g. for "org.apache.logging.log4j:log4j-core", search "org\\.apache\\.logging\\.log4j").
2. Call read_file on each affected file to inspect the actual source code.
3. Identify which API/behavioral changes between {current_version} and {target_version}
   require source-level changes (removed/renamed methods, config format changes).
4. Call apply_file_change for each required edit.
   The "find" value MUST be an exact substring of the file content from read_file — never guess.
   Do NOT edit pom.xml — the version bump is already applied.
5. Call run_maven_compile to verify the changes compile cleanly.
6. If compilation fails: read the error, inspect the affected files, apply corrections, compile again.
7. When compilation succeeds (or if no source changes are needed), return end_turn with JSON.

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
You are a Java/Maven dependency upgrade specialist. A previous fix attempt for this
dependency upgrade FAILED CI. Diagnose the CI failure and apply a corrective fix.

## Dependency being upgraded
- Component: {component_name}
- Current version: {current_version}
- Target version: {target_version}

## Previous CI failure log (root cause of the failure)
```
{failure_log_excerpt}
```

## Repository file tree (paths only)
{file_listing}

## Available tools
- `grep_files(pattern, extensions?)` — regex search across file contents.
- `read_file(relative_path)` — read a file's full content.
- `apply_file_change(relative_path, find, replace, change_description?)` — write a find→replace edit to disk immediately.
- `run_maven_compile()` — run 'mvn compile -q'. No tests. Returns compiler error output on failure.

## Your workflow
1. Analyse the CI failure log to identify the ROOT CAUSE.
2. Use grep_files and read_file to inspect the files mentioned in the failure log.
3. Call apply_file_change for the specific, minimal change that fixes the CI failure.
   Do NOT repeat the same change from the previous attempt unless the log shows it was incomplete.
4. Call run_maven_compile to verify the fix compiles cleanly.
5. If compilation fails: read the error, inspect affected files, apply corrections, compile again.
6. When compilation succeeds, return end_turn with JSON.

## CRITICAL CONSTRAINTS
- Fix only what the CI failure log tells you is broken.
- Do NOT refactor, rename, reformat, or improve unrelated code.
- Do NOT edit pom.xml.
- Never pass a "find" value you have not verified verbatim in read_file output.

```json
{{
  "rationale": "<diagnosis of the CI failure and summary of what was changed>"
}}
```
"""


# ── Exceptions ────────────────────────────────────────────────────────────────

class PomXMLError(Exception):
    """Raised when pom.xml cannot be parsed or the target dependency is not found."""


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

    def __init__(self, repo_path: str, model_deployment_name: Optional[str] = None):
        self._repo_path = Path(repo_path)
        self._model = model_deployment_name or os.environ["MODEL_DEPLOYMENT_NAME"]
        self._client = anthropic.Anthropic()
        self._max_attempts = int(os.environ.get("MAX_RETRY_ATTEMPTS", "3"))
        self._applied_changes: list[str] = []  # paths written by apply_file_change during the loop

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
        tracking_store.update(record)
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
        tracking_store.update(record)
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
        # Step 1: Bump pom.xml with an XML parser — never let the model touch pom.xml directly
        self._bump_pom_version(component_name, current_version, target_version)

        # Step 2: Run the tool-use loop — Claude inspects files, applies changes, verifies compile.
        # Changes are written to disk during the loop via apply_file_change tool calls.
        self._applied_changes = []
        file_listing = self._build_file_listing()
        reasoning, prompt_tokens, completion_tokens = self._call_model(
            component_name=component_name,
            current_version=current_version,
            target_version=target_version,
            file_listing=file_listing,
            failure_log_excerpt=failure_log_excerpt,
        )

        # Deduplicate paths while preserving order — a file may have been corrected more than once.
        files_changed = ["pom.xml"] + list(dict.fromkeys(self._applied_changes))

        return ChangeSummary(
            component_name=component_name,
            old_version=current_version,
            new_version=target_version,
            files_changed=files_changed,
            rationale=reasoning.get("rationale", ""),
            cve_ids=cve_ids,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )

    # ── pom.xml manipulation ──────────────────────────────────────────────────

    def _bump_pom_version(self, component_name: str, current_version: str, target_version: str) -> None:
        """
        Bump a dependency version in pom.xml using an XML parser (never string-replace).

        Handles three real-world patterns found in customer repos:
          1. Standard literal <version>X.Y.Z</version>
          2. Maven property reference <version>${spring.version}</version> — updates the
             property in <properties>, or inlines the version if the property is not found.
          3. BOM-managed dependency (no <version> element) — adds an explicit <version>
             override so the new version is pinned regardless of the BOM.

        Supports both namespaced pom.xml files (xmlns="http://maven.apache.org/POM/4.0.0")
        and bare files without a namespace declaration (some legacy / generated poms).
        """
        pom_path = self._repo_path / "pom.xml"
        if not pom_path.exists():
            raise PomXMLError(f"pom.xml not found at {pom_path}")

        tree = ET.parse(str(pom_path))
        root = tree.getroot()

        ns_uri = "http://maven.apache.org/POM/4.0.0"
        ET.register_namespace("", ns_uri)

        if root.tag.startswith(f"{{{ns_uri}}}"):
            ns      = {"m": ns_uri}
            dep_xpath  = ".//m:dependency"
            tag        = lambda t: f"m:{t}"        # noqa: E731
            subtag     = lambda t: f"{{{ns_uri}}}{t}"  # noqa: E731
            prop_xpath = lambda name: f"./m:properties/m:{name}"  # noqa: E731
        else:
            ns      = {}
            dep_xpath  = ".//dependency"
            tag        = lambda t: t               # noqa: E731
            subtag     = lambda t: t               # noqa: E731
            prop_xpath = lambda name: f"./properties/{name}"  # noqa: E731

        parts       = component_name.split(":")
        artifact_id = parts[-1]
        group_id    = parts[0] if len(parts) > 1 else None

        found = False
        for dep in root.findall(dep_xpath, ns):
            aid_el = dep.find(tag("artifactId"), ns)
            gid_el = dep.find(tag("groupId"),    ns)
            ver_el = dep.find(tag("version"),     ns)

            if aid_el is None:
                continue

            aid_match = aid_el.text == artifact_id
            gid_match = group_id is None or (gid_el is not None and gid_el.text == group_id)
            if not (aid_match and gid_match):
                continue

            if ver_el is None:
                # BOM-managed — add an explicit version override.
                ET.SubElement(dep, subtag("version")).text = target_version
                found = True
                logger.info(
                    "pom.xml: %s added explicit version %s (was BOM-managed)",
                    component_name, target_version,
                )
                break

            ver_text = ver_el.text or ""

            if ver_text.startswith("${") and ver_text.endswith("}"):
                # Maven property reference — update the property value in <properties>.
                prop_name = ver_text[2:-1]
                prop_el = root.find(prop_xpath(prop_name), ns)
                if prop_el is not None:
                    logger.info(
                        "pom.xml: property %s %s → %s",
                        prop_name, prop_el.text, target_version,
                    )
                    prop_el.text = target_version
                else:
                    # Property not found — inline the version directly.
                    logger.info(
                        "pom.xml: %s inlining version (property %s not found)",
                        component_name, prop_name,
                    )
                    ver_el.text = target_version
                found = True
                break

            if ver_text == current_version:
                ver_el.text = target_version
                found = True
                logger.info(
                    "pom.xml: %s %s → %s", component_name, current_version, target_version,
                )
                break

        if not found:
            raise PomXMLError(
                f"Dependency {component_name}@{current_version} not found in pom.xml."
            )

        tree.write(str(pom_path), xml_declaration=True, encoding="utf-8")

    # ── Model call (agentic tool-use loop) ────────────────────────────────────

    def _call_model(
        self,
        component_name: str,
        current_version: str,
        target_version: str,
        file_listing: str,
        failure_log_excerpt: Optional[str],
    ) -> tuple:
        """
        Agentic tool-use loop. Claude calls read_file / grep_files to inspect real
        source before writing find/replace strings, then ends with the JSON answer.
        Returns (reasoning_dict, total_prompt_tokens, total_completion_tokens).

        Token counts are accumulated across all rounds so the tracking record
        reflects the true cost of the entire conversation, not just the last turn.
        """
        if failure_log_excerpt:
            prompt = RETRY_FIX_PROMPT.format(
                component_name=component_name,
                current_version=current_version,
                target_version=target_version,
                failure_log_excerpt=failure_log_excerpt[:6000],
                file_listing=file_listing,
            )
        else:
            prompt = FRESH_FIX_PROMPT.format(
                component_name=component_name,
                current_version=current_version,
                target_version=target_version,
                file_listing=file_listing,
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
        if name == "run_maven_compile":
            return self._tool_run_maven_compile()
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
        exts = set(extensions) if extensions else {".java", ".xml", ".properties", ".yml", ".yaml"}
        try:
            compiled = re.compile(pattern)
        except re.error as exc:
            return f"ERROR: invalid regex {pattern!r}: {exc}"

        results = []
        for f in sorted(self._repo_path.rglob("*")):
            if "target" in f.parts or f.suffix not in exts:
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

    def _tool_run_maven_compile(self) -> str:
        """
        Tool handler: run 'mvn compile -q' in the cloned repo.
        Returns a success message or the full compiler error for Claude to diagnose.
        No tests are executed.
        """
        try:
            result = subprocess.run(
                ["mvn", "compile", "-q", "--batch-mode"],
                cwd=str(self._repo_path),
                capture_output=True,
                text=True,
                timeout=300,
            )
        except FileNotFoundError:
            return "ERROR: mvn not found — Maven must be installed in the container image."
        except subprocess.TimeoutExpired:
            return "ERROR: mvn compile timed out after 300 seconds."

        if result.returncode == 0:
            return "mvn compile: SUCCESS — no compilation errors."

        output = (
            f"mvn compile: FAILED (exit code {result.returncode})\n\n"
            f"STDERR:\n{result.stderr[:10_000]}"
        )
        if result.stdout.strip():
            output += f"\n\nSTDOUT:\n{result.stdout[:5_000]}"
        return output

    # ── File listing (orientation only — content is read via tools) ───────────

    def _build_file_listing(self) -> str:
        files = []
        for ext in ("*.java", "*.xml", "*.properties", "*.yml", "*.yaml"):
            for f in self._repo_path.rglob(ext):
                if "target" not in f.parts:
                    files.append(str(f.relative_to(self._repo_path)))
        return "\n".join(sorted(files)[:200])
