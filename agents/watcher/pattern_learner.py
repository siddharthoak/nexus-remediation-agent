"""
PatternLearner — extracts Tier 1 KB entries from confirmed CI-passing PRs.

Called by the Watcher after a PR reaches CI_PASSED. Fetches the PR file diffs
from GitHub and uses a direct Anthropic API call (not the tool-use loop) to extract
concrete find/replace patterns from the changes. Stores the result as a
"tier1_learned" KnowledgeEntry so future fixes for the same version pair
can skip the LLM and apply patterns mechanically.
"""

import json
import logging
import os
import uuid
from typing import Optional

import anthropic
import requests

from common.knowledge_store import KnowledgeEntry

logger = logging.getLogger(__name__)

PATTERN_EXTRACTION_PROMPT = """\
A pull request fixing a Maven dependency upgrade has just passed CI.
Below are the code changes (unified diff format). Extract find/replace patterns
that can be mechanically applied to future projects upgrading the same dependency.

## Dependency upgraded
- Component: {component_name}
- From: {old_version}  →  To: {new_version}

## PR diff (changes made to pass CI)
```diff
{diff_text}
```

## Instructions
Extract only patterns that are:
1. Deterministic — an exact string that can be found and replaced without context
2. Safe — applying the replacement would not break unrelated code
3. Caused by the version upgrade — not incidental refactoring

Ignore pom.xml changes (version bump is handled separately).
Ignore whitespace-only changes.

Return ONLY valid JSON (no markdown):
{{
  "patterns": [
    {{
      "find": "exact string to find (must be a literal substring that appears verbatim in source)",
      "replace": "replacement string",
      "description": "one sentence explaining why this change is needed"
    }}
  ]
}}

If no safe mechanical patterns can be extracted, return {{"patterns": []}}.
"""


class PatternLearner:
    def __init__(
        self,
        repo_full_name: str,
        github_pat: str,
        model_name: Optional[str] = None,
    ):
        self._repo    = repo_full_name
        self._headers = {
            "Authorization":        f"Bearer {github_pat}",
            "Accept":               "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        self._base       = f"https://api.github.com/repos/{repo_full_name}"
        self._model_name = model_name or os.environ.get("MODEL_DEPLOYMENT_NAME", "claude-haiku-4-5-20251001")
        self._client     = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

    def learn_from_pr(self, pr_number: int, record, kb_store) -> None:
        """
        Fetch the PR diff, extract patterns via Claude, and store a tier1_learned
        KnowledgeEntry. No-op if the entry already exists or extraction yields nothing.
        """
        existing = kb_store.find_applicable(
            record.component_name, record.old_version, record.new_version
        )
        if existing and existing.source == "tier1_learned":
            logger.info(
                "Tier 1 entry already exists for %s %s→%s — skipping.",
                record.component_name, record.old_version, record.new_version,
            )
            return

        diff_text = self._fetch_pr_diff(pr_number)
        if not diff_text:
            logger.info("PatternLearner: no diff for PR #%d — skipping.", pr_number)
            return

        patterns = self._extract_patterns(
            component_name=record.component_name,
            old_version=record.old_version,
            new_version=record.new_version,
            diff_text=diff_text,
        )

        if not patterns:
            logger.info(
                "PatternLearner: no mechanical patterns found in PR #%d for %s.",
                pr_number, record.component_name,
            )
            return

        try:
            from_major = int(record.old_version.split(".")[0])
        except (ValueError, IndexError):
            from_major = -1
        try:
            to_major = int(record.new_version.split(".")[0])
        except (ValueError, IndexError):
            to_major = -1

        entry = KnowledgeEntry(
            entry_id=str(uuid.uuid4()),
            component_name=record.component_name,
            from_version=record.old_version,
            to_version=record.new_version,
            from_major=from_major,
            to_major=to_major,
            source="tier1_learned",
            patterns=patterns,
            confidence="high",
        )

        # If a knowledge_agent entry exists, merge patterns in rather than replacing
        agent_entry = existing  # already fetched above
        if agent_entry and agent_entry.source == "knowledge_agent":
            for p in patterns:
                if not any(e.get("find") == p.get("find") for e in agent_entry.patterns):
                    agent_entry.patterns.append(p)
            agent_entry.source     = "tier1_learned"
            agent_entry.confidence = "high"
            kb_store.update(agent_entry)
            logger.info(
                "PatternLearner: merged %d pattern(s) into existing KB entry for %s.",
                len(patterns), record.component_name,
            )
        else:
            kb_store.create(entry)
            logger.info(
                "PatternLearner: stored %d tier1 pattern(s) for %s %s→%s.",
                len(patterns), record.component_name, record.old_version, record.new_version,
            )

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _fetch_pr_diff(self, pr_number: int) -> str:
        """Return a concatenated unified diff of all non-pom.xml files in the PR."""
        try:
            resp = requests.get(
                f"{self._base}/pulls/{pr_number}/files",
                headers=self._headers,
                timeout=30,
            )
            resp.raise_for_status()
        except Exception as exc:
            logger.warning("PatternLearner: could not fetch PR #%d files: %s", pr_number, exc)
            return ""

        parts = []
        for f in resp.json():
            filename = f.get("filename", "")
            if "pom.xml" in filename:
                continue
            patch = f.get("patch", "")
            if patch:
                parts.append(f"--- {filename}\n{patch}")

        return "\n\n".join(parts)[:8000]

    def _extract_patterns(
        self, component_name: str, old_version: str, new_version: str, diff_text: str
    ) -> list:
        prompt = PATTERN_EXTRACTION_PROMPT.format(
            component_name=component_name,
            old_version=old_version,
            new_version=new_version,
            diff_text=diff_text,
        )
        try:
            response = self._client.messages.create(
                model=self._model_name,
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}],
            )
            data = json.loads(response.content[0].text)
            return [
                p for p in data.get("patterns", [])
                if p.get("find") and p.get("replace") is not None
            ]
        except Exception as exc:
            logger.warning("PatternLearner: extraction failed: %s", exc)
            return []
