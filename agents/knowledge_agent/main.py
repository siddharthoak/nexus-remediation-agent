"""
Knowledge Agent — hydrates the KB for each (component, from_version, to_version) tuple.

Uses a direct Anthropic messages.create() call (one structured extraction per finding).
Not a tool-use loop — just a single prompt-in / JSON-out extraction against fetched
release notes and CVE data.

Called from the fixer's _do_fresh_scan() before the classifier runs.
Skips findings already covered by a Tier 2 playbook or a prior knowledge_agent entry.
"""

import json
import logging
import os
import uuid
from typing import Optional

import anthropic

from knowledge_agent.retrieval import ReleaseFetcher
from common.knowledge_store import KnowledgeEntry

logger = logging.getLogger(__name__)

EXTRACTION_PROMPT = """\
You are a Maven dependency migration analyst. Based on the release notes and CVE data below,
extract structured migration information for upgrading {component_name} from version
{from_version} to {to_version}.

## Source data
{source_data}

## Instructions
Identify:
1. Breaking changes — API removals, renamed classes/methods, config format changes, behavioral changes
2. API removals — fully-qualified class/method names removed in this upgrade range
3. Migration steps — concrete ordered steps to migrate source code
4. Find/replace patterns — exact Java import or code patterns that can be mechanically replaced

Return ONLY valid JSON in this exact format (no markdown, no prose):
{{
  "breaking_changes": ["string", ...],
  "api_removals": ["string", ...],
  "migration_steps": ["string", ...],
  "patterns": [
    {{"find": "exact string to find", "replace": "replacement string", "description": "why"}}
  ],
  "confidence": "high|medium|low"
}}

If the source data does not contain enough information to populate a field, use an empty list.
Confidence: "high" if release notes are authoritative, "medium" if inferred, "low" if speculative.
"""


class KnowledgeAgent:
    """
    Hydrates the KB for all findings in a scan that don't already have an entry.
    Skips findings already covered by a Tier 2 playbook or a prior knowledge_agent entry.
    """

    def __init__(
        self,
        github_pat: Optional[str] = None,
        model_name: Optional[str] = None,
    ):
        self._fetcher    = ReleaseFetcher(github_pat=github_pat)
        self._model_name = model_name or os.environ.get("MODEL_DEPLOYMENT_NAME", "claude-haiku-4-5-20251001")
        self._client     = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

    def hydrate(self, findings: list, kb_store) -> None:
        """
        For each finding not already in the KB, fetch release data and extract a KnowledgeEntry.
        Deduplicates by (component_name, from_version, to_version) across the findings list.
        """
        if os.environ.get("KB_HYDRATION", "1") != "1":
            logger.info("KnowledgeAgent: KB_HYDRATION disabled — skipping.")
            return

        seen = set()
        for finding in findings:
            key = (finding.component_name, finding.current_version, finding.recommended_version)
            if key in seen:
                continue
            seen.add(key)

            existing = kb_store.find_applicable(
                finding.component_name, finding.current_version, finding.recommended_version
            )
            if existing:
                logger.info(
                    "KnowledgeAgent: KB hit (%s) for %s — skipping research.",
                    existing.source, finding.component_name,
                )
                continue

            logger.info(
                "KnowledgeAgent: researching %s %s→%s",
                finding.component_name, finding.current_version, finding.recommended_version,
            )
            entry = self._research(finding)
            if entry:
                kb_store.create(entry)
                logger.info(
                    "KnowledgeAgent: stored entry for %s (confidence=%s, "
                    "%d breaking changes, %d patterns)",
                    finding.component_name, entry.confidence,
                    len(entry.breaking_changes), len(entry.patterns),
                )
            else:
                logger.info(
                    "KnowledgeAgent: no useful data found for %s — KB entry skipped.",
                    finding.component_name,
                )

    def _research(self, finding) -> Optional[KnowledgeEntry]:
        source_data = self._fetcher.fetch(
            component_name=finding.component_name,
            from_version=finding.current_version,
            to_version=finding.recommended_version,
            cve_ids=finding.cve_ids,
        )

        if not source_data.strip():
            logger.debug(
                "KnowledgeAgent: no source data fetched for %s — skipping extraction.",
                finding.component_name,
            )
            return None

        prompt = EXTRACTION_PROMPT.format(
            component_name=finding.component_name,
            from_version=finding.current_version,
            to_version=finding.recommended_version,
            source_data=source_data[:8000],
        )

        try:
            response = self._client.messages.create(
                model=self._model_name,
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}],
            )
            data = json.loads(response.content[0].text)
        except json.JSONDecodeError as exc:
            logger.warning(
                "KnowledgeAgent: JSON parse failed for %s: %s",
                finding.component_name, exc,
            )
            return None
        except Exception as exc:
            logger.warning(
                "KnowledgeAgent: extraction failed for %s: %s",
                finding.component_name, exc,
            )
            return None

        # Skip entries with no useful content
        if not any([data.get("breaking_changes"), data.get("patterns"), data.get("migration_steps")]):
            return None

        try:
            from_major = int(finding.current_version.split(".")[0])
        except (ValueError, IndexError):
            from_major = -1
        try:
            to_major = int(finding.recommended_version.split(".")[0])
        except (ValueError, IndexError):
            to_major = -1

        return KnowledgeEntry(
            entry_id=str(uuid.uuid4()),
            component_name=finding.component_name,
            from_version=finding.current_version,
            to_version=finding.recommended_version,
            from_major=from_major,
            to_major=to_major,
            source="knowledge_agent",
            breaking_changes=data.get("breaking_changes", []),
            api_removals=data.get("api_removals", []),
            migration_steps=data.get("migration_steps", []),
            patterns=data.get("patterns", []),
            confidence=data.get("confidence", "low"),
        )
