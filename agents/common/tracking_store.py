"""
Shared tracking store — the single source of truth for all fix attempt state.

Both the Fixer and Watcher read and write this store. Neither agent makes decisions
based solely on its own in-process state; all state transitions go through here.

Tracking lineage model:
  - Each fix attempt (fresh OR retry) gets its own TrackingRecord with a unique tracking_id.
  - Retries chain back to the original via parent_tracking_id, forming a linked list.
  - pr_number is the stable join key: once set, every record in the same retry lineage
    carries the same pr_number. Counting records by pr_number gives total attempts.

Status machine (only valid forward transitions are enforced):
  CREATED → PR_OPENED → CI_PENDING → CI_PASSED          (happy path)
                                    → CI_FAILED → RETRY_REQUESTED   (Watcher requests retry)
                                    → FAILED_MAX_RETRIES             (Watcher hits bound)
                                    → ESCALATED                      (human flagged)
  RETRY_REQUESTED → (Fixer picks up the record and creates a new child with CREATED)

Two backends:
  - CosmosTrackingStore  — production, persists across container restarts
  - InMemoryTrackingStore — testing, no external dependencies
"""

import logging
import os
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

# ── Status enum ───────────────────────────────────────────────────────────────

class TrackingStatus(str, Enum):
    CREATED              = "CREATED"
    PR_OPENED            = "PR_OPENED"
    CI_PENDING           = "CI_PENDING"
    CI_PASSED            = "CI_PASSED"
    CI_FAILED            = "CI_FAILED"
    RETRY_REQUESTED      = "RETRY_REQUESTED"   # Set by Watcher; only Fixer may act on this
    FAILED_MAX_RETRIES   = "FAILED_MAX_RETRIES"
    ESCALATED            = "ESCALATED"
    SKIPPED              = "SKIPPED"           # Set by Classifier for Bucket 1/4 — terminal, Issue created
    IGNORED              = "IGNORED"           # [DEFERRED] Discovery: matched ignore_list.yaml
    KNOWN_BLOCKED        = "KNOWN_BLOCKED"     # [DEFERRED] Discovery: matched known_list.yaml


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class TokenUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0


@dataclass
class TrackingRecord:
    tracking_id: str                         # UUID, unique per attempt
    vulnerability_id: str                    # CVE ID or component identifier
    repo: str                                # "org/repo"
    component_name: str
    old_version: str
    new_version: str
    status: str                              # TrackingStatus value
    created_at: str                          # ISO 8601
    updated_at: str

    parent_tracking_id: Optional[str] = None # None for first attempt; points to previous attempt
    pr_number: Optional[int] = None          # None until Fixer creates the PR
    branch_name: str = ""
    attempt_number: int = 1                  # 1 for original; 2, 3 … for retries

    time_to_resolution_seconds: Optional[float] = None  # Set when CI_PASSED or FAILED_MAX_RETRIES
    token_usage: Optional[dict] = None       # {"prompt_tokens": N, "completion_tokens": N}
    failure_log_excerpt: Optional[str] = None  # Written by Watcher; read by Fixer on retry
    framework_detected: Optional[str] = None  # e.g. "maven", "gradle", "npm", "python"; set by Fixer at startup
    unit_test_status: Optional[str] = None   # "SUCCESS" | "NO_TESTS_FOUND" | "SOFT_FAIL"; set at end_turn
    bucket: Optional[int] = None             # 1-4; set by Classifier after KB hydration
    skip_reason: Optional[str] = None        # Set by Classifier (Bucket 1/4); included in GitHub Issue body
    kb_hit: Optional[bool] = None            # True if the Fixer applied stored KB patterns with no model call


# ── Protocol ──────────────────────────────────────────────────────────────────

@runtime_checkable
class TrackingStoreProtocol(Protocol):
    def create(self, record: TrackingRecord) -> None: ...
    def get(self, tracking_id: str) -> Optional[TrackingRecord]: ...
    def get_latest_for_pr(self, pr_number: int) -> Optional[TrackingRecord]: ...
    def get_lineage(self, pr_number: int) -> list: ...
    def get_all(self) -> list: ...
    def count_attempts_for_pr(self, pr_number: int) -> int: ...
    def update(self, record: TrackingRecord) -> None: ...


# ── Factory helpers ───────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_tracking_id() -> str:
    return str(uuid.uuid4())


def make_fresh_record(
    vulnerability_id: str,
    repo: str,
    component_name: str,
    old_version: str,
    new_version: str,
) -> TrackingRecord:
    """Create a brand-new tracking record for a first fix attempt."""
    now = _now()
    return TrackingRecord(
        tracking_id=new_tracking_id(),
        vulnerability_id=vulnerability_id,
        repo=repo,
        component_name=component_name,
        old_version=old_version,
        new_version=new_version,
        status=TrackingStatus.CREATED.value,
        created_at=now,
        updated_at=now,
        parent_tracking_id=None,
        attempt_number=1,
    )


def make_retry_record(
    parent: TrackingRecord,
    failure_log_excerpt: str,
) -> TrackingRecord:
    """
    Create a child tracking record for a retry attempt.
    Called exclusively by the Watcher when it decides to request a retry.
    The status is set to RETRY_REQUESTED here — the Fixer validates this before acting.
    """
    now = _now()
    return TrackingRecord(
        tracking_id=new_tracking_id(),
        vulnerability_id=parent.vulnerability_id,
        repo=parent.repo,
        component_name=parent.component_name,
        old_version=parent.old_version,
        new_version=parent.new_version,
        status=TrackingStatus.RETRY_REQUESTED.value,
        created_at=now,
        updated_at=now,
        parent_tracking_id=parent.tracking_id,
        pr_number=parent.pr_number,
        branch_name=parent.branch_name,
        attempt_number=parent.attempt_number + 1,
        failure_log_excerpt=failure_log_excerpt[:4000] if failure_log_excerpt else None,
    )


# ── In-memory backend (testing / local dev) ───────────────────────────────────

class InMemoryTrackingStore:
    """Testing backend — no external dependencies. Not safe across container restarts."""

    def __init__(self):
        self._records: dict = {}  # tracking_id → TrackingRecord

    def create(self, record: TrackingRecord) -> None:
        self._records[record.tracking_id] = record

    def get(self, tracking_id: str) -> Optional[TrackingRecord]:
        return self._records.get(tracking_id)

    def get_latest_for_pr(self, pr_number: int) -> Optional[TrackingRecord]:
        matches = [r for r in self._records.values() if r.pr_number == pr_number]
        if not matches:
            return None
        return sorted(matches, key=lambda r: r.attempt_number, reverse=True)[0]

    def get_lineage(self, pr_number: int) -> list:
        """Return all attempts for a PR, ordered by attempt_number ascending."""
        matches = [r for r in self._records.values() if r.pr_number == pr_number]
        return sorted(matches, key=lambda r: r.attempt_number)

    def get_all(self) -> list:
        return list(self._records.values())

    def count_attempts_for_pr(self, pr_number: int) -> int:
        return sum(1 for r in self._records.values() if r.pr_number == pr_number)

    def update(self, record: TrackingRecord) -> None:
        record.updated_at = _now()
        self._records[record.tracking_id] = record

    def all_with_status(self, status: str) -> list:
        return [r for r in self._records.values() if r.status == status]


# ── Cosmos DB backend (production) ───────────────────────────────────────────

DEFAULT_DATABASE  = "oss-remediation"
DEFAULT_CONTAINER = "tracking-records"


class CosmosTrackingStore:
    """
    Production backend backed by Azure Cosmos DB (NoSQL API).
    Uses the Watcher/Fixer agent's managed identity (DefaultAzureCredential) — no keys.

    Document id  = tracking_id (UUID)
    Partition key = tracking_id (single-document partitions — simple, even distribution)

    Cross-partition queries are used only for get_latest_for_pr / count_attempts_for_pr,
    which are low-frequency operations (once per CI failure event).
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
        db_name  = database_name  or os.environ.get("COSMOS_DATABASE",  DEFAULT_DATABASE)
        ctr_name = container_name or os.environ.get("COSMOS_CONTAINER", DEFAULT_CONTAINER)

        client = CosmosClient(url=endpoint, credential=DefaultAzureCredential())
        self._container = client.get_database_client(db_name).get_container_client(ctr_name)

    def create(self, record: TrackingRecord) -> None:
        doc = self._to_doc(record)
        self._container.create_item(doc)
        logger.debug("TrackingStore: created %s status=%s", record.tracking_id[:8], record.status)

    def get(self, tracking_id: str) -> Optional[TrackingRecord]:
        from azure.cosmos import exceptions as cosmos_exc
        try:
            doc = self._container.read_item(item=tracking_id, partition_key=tracking_id)
            return self._from_doc(doc)
        except cosmos_exc.CosmosResourceNotFoundError:
            return None

    def get_latest_for_pr(self, pr_number: int) -> Optional[TrackingRecord]:
        """Return the highest-attempt-number record for this PR."""
        results = list(self._container.query_items(
            query=(
                "SELECT * FROM c WHERE c.pr_number = @pr "
                "ORDER BY c.attempt_number DESC OFFSET 0 LIMIT 1"
            ),
            parameters=[{"name": "@pr", "value": pr_number}],
            enable_cross_partition_query=True,
        ))
        return self._from_doc(results[0]) if results else None

    def count_attempts_for_pr(self, pr_number: int) -> int:
        results = list(self._container.query_items(
            query="SELECT VALUE COUNT(1) FROM c WHERE c.pr_number = @pr",
            parameters=[{"name": "@pr", "value": pr_number}],
            enable_cross_partition_query=True,
        ))
        return results[0] if results else 0

    def update(self, record: TrackingRecord) -> None:
        record.updated_at = _now()
        # Use ETag from last read for optimistic concurrency if available.
        # For simplicity in the store interface we do a full upsert here;
        # callers should read → mutate → update in one logical step.
        self._container.upsert_item(self._to_doc(record))

    def get_lineage(self, pr_number: int) -> list:
        """Return all attempts for a PR, ordered by attempt_number ascending."""
        results = list(self._container.query_items(
            query="SELECT * FROM c WHERE c.pr_number = @pr ORDER BY c.attempt_number ASC",
            parameters=[{"name": "@pr", "value": pr_number}],
            enable_cross_partition_query=True,
        ))
        return [self._from_doc(d) for d in results]

    def get_all(self) -> list:
        results = self._container.query_items(
            query="SELECT * FROM c",
            enable_cross_partition_query=True,
        )
        return [self._from_doc(d) for d in results]

    def get_all_with_status(self, status: str) -> list:
        results = self._container.query_items(
            query="SELECT * FROM c WHERE c.status = @status",
            parameters=[{"name": "@status", "value": status}],
            enable_cross_partition_query=True,
        )
        return [self._from_doc(d) for d in results]

    # ── Serialisation ─────────────────────────────────────────────────────────

    @staticmethod
    def _to_doc(record: TrackingRecord) -> dict:
        d = asdict(record)
        d["id"] = record.tracking_id   # Cosmos requires "id" field
        return d

    @staticmethod
    def _from_doc(doc: dict) -> TrackingRecord:
        doc.pop("id", None)
        # Strip Cosmos internal fields
        for key in ("_rid", "_self", "_etag", "_attachments", "_ts"):
            doc.pop(key, None)
        return TrackingRecord(**doc)


# ── Store factory ─────────────────────────────────────────────────────────────

def make_tracking_store() -> "InMemoryTrackingStore | CosmosTrackingStore":
    """
    Return the appropriate store based on DEPLOYMENT_MODE.

    DEPLOYMENT_MODE=azure (or COSMOS_ENDPOINT set without explicit mode) → CosmosTrackingStore
    DEPLOYMENT_MODE=local (or no COSMOS_ENDPOINT)                        → InMemoryTrackingStore

    InMemoryTrackingStore loses all state on container restart and is only appropriate
    for local development and CI unit tests. Set DEPLOYMENT_MODE=local explicitly to
    suppress the warning when running outside Azure.
    """
    mode = os.environ.get("DEPLOYMENT_MODE", "")
    use_cosmos = mode == "azure" or (not mode and bool(os.environ.get("COSMOS_ENDPOINT")))
    if use_cosmos:
        return CosmosTrackingStore()
    if mode == "local":
        logger.info("DEPLOYMENT_MODE=local — using InMemoryTrackingStore.")
    else:
        logger.warning(
            "COSMOS_ENDPOINT not set and DEPLOYMENT_MODE unspecified — "
            "using InMemoryTrackingStore. State will be lost on restart. "
            "Set DEPLOYMENT_MODE=local to suppress this warning."
        )
    return InMemoryTrackingStore()
