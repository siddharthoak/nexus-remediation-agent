"""
Knowledge Store — persists KB entries for (component, from_version, to_version) tuples.

Three tiers, all stored in the same backend:
  tier2_playbook    — loaded at startup from playbooks/*.yaml (engineer-authored)
  knowledge_agent   — written by the Knowledge Agent from web-fetched release notes
  tier1_learned     — written by the Watcher after a CI_PASSED fix is confirmed

Lookup priority: tier1_learned > tier2_playbook > knowledge_agent

Backends (selected by DEPLOYMENT_MODE env var):
  InMemoryKBStore  — local dev / CI testing (DEPLOYMENT_MODE=local or no COSMOS_ENDPOINT)
  CosmosKBStore    — Azure production (DEPLOYMENT_MODE=azure or COSMOS_ENDPOINT set)

The file-based backend used in local prototypes is intentionally absent here. The
nexus-remediation-agent is designed for Azure AI Foundry deployment; KB entries must
survive container restarts and be shared across all agent instances, which requires
Cosmos DB. For local development, InMemoryKBStore is sufficient (preloaded from playbooks).
"""

import logging
import os
import uuid
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Protocol, runtime_checkable

import yaml

logger = logging.getLogger(__name__)

PLAYBOOKS_DIR = Path(__file__).parent.parent.parent / "playbooks"
TIER_PRIORITY = {"tier1_learned": 3, "tier2_playbook": 2, "knowledge_agent": 1}

_COSMOS_DEFAULT_DATABASE  = "oss-remediation"
_COSMOS_DEFAULT_CONTAINER = "kb-entries"


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class KnowledgeEntry:
    entry_id: str
    component_name: str
    from_version: str           # exact from-version, or "" for major-version playbooks
    to_version: str             # exact to-version, or "" for major-version playbooks
    from_major: int             # parsed major version of from_version
    to_major: int               # parsed major version of to_version
    source: str                 # tier1_learned | tier2_playbook | knowledge_agent
    breaking_changes: list = field(default_factory=list)
    api_removals: list = field(default_factory=list)
    migration_steps: list = field(default_factory=list)
    patterns: list = field(default_factory=list)   # [{"find":"","replace":"","description":""}]
    confidence: str = "medium"  # high | medium | low
    created_at: str = ""


# ── Protocol ──────────────────────────────────────────────────────────────────

@runtime_checkable
class KnowledgeStoreProtocol(Protocol):
    def create(self, entry: KnowledgeEntry) -> None: ...
    def get(self, component_name: str, from_version: str, to_version: str) -> Optional[KnowledgeEntry]: ...
    def find_applicable(self, component_name: str, from_version: str, to_version: str) -> Optional[KnowledgeEntry]: ...
    def update(self, entry: KnowledgeEntry) -> None: ...
    def get_all(self) -> list: ...


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_major(version: str) -> int:
    try:
        return int(version.split(".")[0])
    except (ValueError, IndexError, AttributeError):
        return -1


def _component_stem(component_name: str) -> str:
    """Return just the artifactId part of a groupId:artifactId component name."""
    return component_name.split(":")[-1].lower()


def _load_playbooks() -> list:
    """Load all Tier 2 YAML playbooks from the playbooks/ directory."""
    entries = []
    if not PLAYBOOKS_DIR.exists():
        return entries
    for path in sorted(PLAYBOOKS_DIR.glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            entry = KnowledgeEntry(
                entry_id=f"playbook:{path.stem}",
                component_name=data["component"],
                from_version="",
                to_version="",
                from_major=int(data.get("from_major", -1)),
                to_major=int(data.get("to_major", -1)),
                source="tier2_playbook",
                breaking_changes=data.get("breaking_changes", []),
                api_removals=data.get("api_removals", []),
                migration_steps=data.get("migration_steps", []),
                patterns=data.get("patterns", []),
                confidence=data.get("confidence", "high"),
                created_at=_now(),
            )
            entries.append(entry)
            logger.debug("Loaded playbook: %s", path.name)
        except Exception as exc:
            logger.warning("Could not load playbook %s: %s", path.name, exc)
    return entries


# ── Lookup logic ──────────────────────────────────────────────────────────────

def _find_best(
    candidates: list,
    component_name: str,
    from_version: str,
    to_version: str,
) -> Optional[KnowledgeEntry]:
    """
    Scoring (higher wins):
      1000+tier — exact (component, from_version, to_version) match
      100+tier  — same component, matching major-version range
      10+tier   — component-stem match on major-version range
    Within each band, tier1_learned > tier2_playbook > knowledge_agent.
    """
    from_major = _parse_major(from_version)
    to_major   = _parse_major(to_version)
    stem       = _component_stem(component_name)

    def score(e: KnowledgeEntry) -> int:
        tier = TIER_PRIORITY.get(e.source, 0)

        if (e.component_name == component_name
                and e.from_version == from_version
                and e.to_version == to_version):
            return 1000 + tier

        if (e.component_name == component_name
                and (e.from_major == -1 or e.from_major == from_major)
                and (e.to_major == -1 or e.to_major == to_major)):
            return 100 + tier

        entry_stem  = _component_stem(e.component_name)
        stems_match = stem.startswith(entry_stem) or entry_stem.startswith(stem)
        majors_ok   = (
            (e.from_major == -1 or e.from_major == from_major)
            and (e.to_major == -1 or e.to_major == to_major)
        )
        if stems_match and majors_ok:
            return 10 + tier

        return 0

    scored = [(score(e), e) for e in candidates if score(e) > 0]
    return max(scored, key=lambda x: x[0])[1] if scored else None


# ── In-memory backend (local dev / CI testing) ────────────────────────────────

class InMemoryKBStore:
    """No external dependencies. Playbooks are loaded from disk at init time."""

    def __init__(self):
        self._entries: dict = {}
        for e in _load_playbooks():
            self._entries[e.entry_id] = e

    def create(self, entry: KnowledgeEntry) -> None:
        if not entry.entry_id:
            entry.entry_id = str(uuid.uuid4())
        if not entry.created_at:
            entry.created_at = _now()
        self._entries[entry.entry_id] = entry

    def get(self, component_name: str, from_version: str, to_version: str) -> Optional[KnowledgeEntry]:
        for e in self._entries.values():
            if (e.component_name == component_name
                    and e.from_version == from_version
                    and e.to_version == to_version):
                return e
        return None

    def find_applicable(self, component_name: str, from_version: str, to_version: str) -> Optional[KnowledgeEntry]:
        return _find_best(list(self._entries.values()), component_name, from_version, to_version)

    def update(self, entry: KnowledgeEntry) -> None:
        self._entries[entry.entry_id] = entry

    def get_all(self) -> list:
        return list(self._entries.values())


# ── Cosmos DB backend (Azure AI Foundry production) ───────────────────────────

class CosmosKBStore:
    """
    Azure Cosmos DB backend for KB persistence.

    Uses the agent's managed identity (DefaultAzureCredential) — no connection strings.
    Playbooks are merged at query time and always win over same-tier remote entries.

    Container: kb-entries (configured via COSMOS_KB_CONTAINER env var)
    Partition key: /entry_id — single-document partitions, even distribution.
    No TTL — KB entries are long-lived and accumulate value over time.
    """

    def __init__(
        self,
        cosmos_endpoint: Optional[str] = None,
        database_name: Optional[str] = None,
        container_name: Optional[str] = None,
    ):
        from azure.cosmos import CosmosClient
        from azure.identity import DefaultAzureCredential

        endpoint = cosmos_endpoint or os.environ["COSMOS_ENDPOINT"]
        db_name  = database_name  or os.environ.get("COSMOS_DATABASE",     _COSMOS_DEFAULT_DATABASE)
        ctr_name = container_name or os.environ.get("COSMOS_KB_CONTAINER", _COSMOS_DEFAULT_CONTAINER)

        client = CosmosClient(url=endpoint, credential=DefaultAzureCredential())
        self._container = client.get_database_client(db_name).get_container_client(ctr_name)

    def create(self, entry: KnowledgeEntry) -> None:
        if not entry.entry_id:
            entry.entry_id = str(uuid.uuid4())
        if not entry.created_at:
            entry.created_at = _now()
        doc = self._to_doc(entry)
        self._container.create_item(doc)
        logger.debug("CosmosKBStore: created %s (%s)", entry.entry_id[:8], entry.component_name)

    def get(self, component_name: str, from_version: str, to_version: str) -> Optional[KnowledgeEntry]:
        results = list(self._container.query_items(
            query=(
                "SELECT * FROM c WHERE c.component_name = @cn "
                "AND c.from_version = @fv AND c.to_version = @tv OFFSET 0 LIMIT 1"
            ),
            parameters=[
                {"name": "@cn",  "value": component_name},
                {"name": "@fv",  "value": from_version},
                {"name": "@tv",  "value": to_version},
            ],
            enable_cross_partition_query=True,
        ))
        return self._from_doc(results[0]) if results else None

    def find_applicable(self, component_name: str, from_version: str, to_version: str) -> Optional[KnowledgeEntry]:
        remote = list(self._container.query_items(
            query="SELECT * FROM c WHERE c.component_name = @cn",
            parameters=[{"name": "@cn", "value": component_name}],
            enable_cross_partition_query=True,
        ))
        candidates = [self._from_doc(d) for d in remote] + _load_playbooks()
        return _find_best(candidates, component_name, from_version, to_version)

    def update(self, entry: KnowledgeEntry) -> None:
        self._container.upsert_item(self._to_doc(entry))

    def get_all(self) -> list:
        remote = [self._from_doc(d) for d in self._container.query_items(
            query="SELECT * FROM c",
            enable_cross_partition_query=True,
        )]
        # Merge playbooks: add any whose entry_id is not already in the remote set
        remote_ids = {e.entry_id for e in remote}
        for pb in _load_playbooks():
            if pb.entry_id not in remote_ids:
                remote.append(pb)
        return remote

    # ── Serialisation ─────────────────────────────────────────────────────────

    @staticmethod
    def _to_doc(entry: KnowledgeEntry) -> dict:
        d = asdict(entry)
        d["id"] = entry.entry_id
        return d

    @staticmethod
    def _from_doc(doc: dict) -> KnowledgeEntry:
        doc.pop("id", None)
        for k in ("_rid", "_self", "_etag", "_attachments", "_ts"):
            doc.pop(k, None)
        return KnowledgeEntry(**doc)


# ── Factory ───────────────────────────────────────────────────────────────────

def make_knowledge_store() -> "InMemoryKBStore | CosmosKBStore":
    """
    Return the appropriate KB store based on DEPLOYMENT_MODE.

    DEPLOYMENT_MODE=azure (or COSMOS_ENDPOINT set without explicit mode) → CosmosKBStore
    DEPLOYMENT_MODE=local (or no COSMOS_ENDPOINT)                        → InMemoryKBStore

    InMemoryKBStore is preloaded with Tier 2 playbooks from disk, so local runs are
    not completely empty — they just won't persist knowledge_agent or tier1_learned entries
    across restarts.
    """
    mode = os.environ.get("DEPLOYMENT_MODE", "")
    use_cosmos = mode == "azure" or (not mode and bool(os.environ.get("COSMOS_ENDPOINT")))
    if use_cosmos:
        logger.info("CosmosKBStore: connecting to %s", os.environ.get("COSMOS_ENDPOINT", "?"))
        return CosmosKBStore()
    logger.warning(
        "DEPLOYMENT_MODE=local (or COSMOS_ENDPOINT not set) — "
        "using InMemoryKBStore. KB entries will not persist across restarts."
    )
    return InMemoryKBStore()
