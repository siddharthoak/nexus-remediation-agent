"""
Classifier — assigns each VulnerabilityFinding to a processing bucket (1–4).

Pure Python: no LLM calls, no network I/O. Reads only from the KB store.

Bucket definitions:
  1 — No fix path: recommended_version is UNKNOWN, or no fix listed. Create GitHub Issue, skip fixer.
  2 — Patch / minor: same major version OR major upgrade without the complex-framework flag. Run fixer.
  3 — Major + KB: major version delta with a KB entry available. Run fixer with KB context injected.
  4 — Complex framework + major + no KB: create GitHub Issue for human triage, skip fixer.
"""

import logging
from dataclasses import dataclass
from typing import Optional

from common.knowledge_store import KnowledgeEntry

logger = logging.getLogger(__name__)

# Components where a major-version upgrade without a KB entry triggers human triage.
COMPLEX_FRAMEWORKS = frozenset({
    "spring-boot",
    "spring-core",
    "spring-web",
    "spring-webmvc",
    "spring-context",
    "spring-framework",
    "hibernate-core",
    "hibernate-entitymanager",
    "struts2-core",
    "jersey-server",
    "jersey-common",
    "resteasy-jaxrs",
    "wicket-core",
    "jsf-api",
    "myfaces-impl",
})


@dataclass
class ClassifierResult:
    bucket: int
    rationale: str
    kb_entry: Optional[KnowledgeEntry] = None


class Classifier:
    def __init__(self, kb_store):
        self._kb = kb_store

    def classify(self, finding) -> ClassifierResult:
        """
        Classify a VulnerabilityFinding into one of four buckets.
        Returns a ClassifierResult with bucket, human-readable rationale, and kb_entry if found.
        """
        component = finding.component_name
        old_ver   = finding.current_version
        new_ver   = finding.recommended_version

        # ── Bucket 1: no fix available ────────────────────────────────────────
        if _is_unknown_version(new_ver):
            return ClassifierResult(
                bucket=1,
                rationale=(
                    f"No safe version identified for {component}. "
                    f"Scanner reported: '{new_ver}'. Manual triage required."
                ),
            )

        old_major  = _parse_major(old_ver)
        new_major  = _parse_major(new_ver)
        is_major   = old_major != -1 and new_major != -1 and new_major > old_major

        kb_entry   = self._kb.find_applicable(component, old_ver, new_ver)
        stem       = _component_stem(component)
        is_complex = any(f in stem for f in COMPLEX_FRAMEWORKS)

        # ── Bucket 4: complex framework + major + no KB ───────────────────────
        if is_complex and is_major and kb_entry is None:
            return ClassifierResult(
                bucket=4,
                rationale=(
                    f"{component} is a complex framework with a major-version upgrade "
                    f"({old_ver} → {new_ver}) and no KB entry or migration playbook. "
                    "Automated fix is likely to be incomplete or incorrect."
                ),
            )

        # ── Bucket 3: major version with KB ──────────────────────────────────
        if is_major and kb_entry is not None:
            breaking = len(kb_entry.breaking_changes)
            patterns = len(kb_entry.patterns)
            return ClassifierResult(
                bucket=3,
                rationale=(
                    f"Major-version upgrade ({old_ver} → {new_ver}) with "
                    f"{kb_entry.source} KB entry: "
                    f"{breaking} breaking change(s), {patterns} fix pattern(s)."
                ),
                kb_entry=kb_entry,
            )

        # ── Bucket 2: patch/minor (or major without complex-framework flag) ───
        upgrade_type = "major" if is_major else ("minor" if _is_minor(old_ver, new_ver) else "patch")
        kb_note      = f"KB entry ({kb_entry.source}) available." if kb_entry else "No KB entry."
        return ClassifierResult(
            bucket=2,
            rationale=f"{upgrade_type.capitalize()}-version upgrade ({old_ver} → {new_ver}). {kb_note}",
            kb_entry=kb_entry,
        )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _is_unknown_version(version: str) -> bool:
    return version.upper().startswith("UNKNOWN") or not version.strip()


def _parse_major(version: str) -> int:
    try:
        return int(version.strip().lstrip("v").split(".")[0])
    except (ValueError, IndexError, AttributeError):
        return -1


def _is_minor(old_ver: str, new_ver: str) -> bool:
    try:
        old_parts = old_ver.strip().lstrip("v").split(".")
        new_parts = new_ver.strip().lstrip("v").split(".")
        return int(old_parts[0]) == int(new_parts[0]) and int(new_parts[1]) > int(old_parts[1])
    except (ValueError, IndexError):
        return False


def _component_stem(component_name: str) -> str:
    return component_name.split(":")[-1].lower()
