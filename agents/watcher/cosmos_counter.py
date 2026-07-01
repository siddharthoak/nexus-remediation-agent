"""
CosmosDB-backed attempt counter — production replacement for InMemoryAttemptCounter.

Each PR gets one document in a Cosmos container. The document stores the attempt count
and metadata (repo, branch, first/last attempt timestamps) so you can query history.

Document schema:
  {
    "id": "org/repo#42",          ← partition key + document id
    "pr_number": 42,
    "repo": "org/repo",
    "branch": "fix/log4j-abc123",
    "attempt_count": 2,
    "first_attempt_at": "2024-01-15T10:00:00Z",
    "last_attempt_at": "2024-01-15T10:45:00Z",
    "status": "in_progress"       ← in_progress | escalated | resolved
  }

Setup:
  1. The Cosmos account + database + container are provisioned by infra/main.bicep (see INF-01 update).
  2. The Watcher agent's managed identity is granted the "Cosmos DB Built-in Data Contributor"
     role on the container — wired in bootstrap_foundry_project.sh.
  3. Set env var COSMOS_ENDPOINT (e.g. https://oss-remediation.documents.azure.com:443/)
     and COSMOS_DATABASE / COSMOS_CONTAINER (defaults below).

Usage in watcher/main.py:
  from cosmos_counter import CosmosAttemptCounter
  counter = CosmosAttemptCounter(repo_full_name="org/repo")
  controller = RetryController(..., attempt_counter=counter)
"""

import logging
import os
from datetime import datetime, timezone
from typing import Optional

from azure.cosmos import CosmosClient, PartitionKey, exceptions as cosmos_exc
from azure.identity import DefaultAzureCredential

logger = logging.getLogger(__name__)

DEFAULT_DATABASE = "oss-remediation"
DEFAULT_CONTAINER = "retry-attempts"


class CosmosAttemptCounter:
    """
    Persistent attempt counter backed by Azure Cosmos DB (NoSQL API).

    Implements the same AttemptCounter protocol as InMemoryAttemptCounter so it
    can be dropped in without changing RetryController at all.

    Uses optimistic concurrency (ETag) to handle the unlikely but possible case
    of two Watcher instances running concurrently for the same PR.
    """

    def __init__(
        self,
        repo_full_name: str,
        cosmos_endpoint: Optional[str] = None,
        database_name: Optional[str] = None,
        container_name: Optional[str] = None,
    ):
        self._repo = repo_full_name
        endpoint = cosmos_endpoint or os.environ["COSMOS_ENDPOINT"]
        db_name = database_name or os.environ.get("COSMOS_DATABASE", DEFAULT_DATABASE)
        container_name = container_name or os.environ.get("COSMOS_CONTAINER", DEFAULT_CONTAINER)

        # Use managed identity (DefaultAzureCredential) — no connection strings / keys
        credential = DefaultAzureCredential()
        client = CosmosClient(url=endpoint, credential=credential)
        db = client.get_database_client(db_name)
        self._container = db.get_container_client(container_name)

    # ── AttemptCounter protocol ───────────────────────────────────────────────

    def get(self, pr_number: int) -> int:
        """Return the current attempt count for `pr_number`, or 0 if no record exists."""
        doc = self._read_doc(pr_number)
        return doc.get("attempt_count", 0) if doc else 0

    def increment(self, pr_number: int) -> int:
        """
        Atomically increment the attempt count for `pr_number`.
        Creates the document if it doesn't exist yet.
        Returns the new count after incrementing.
        Uses ETag-based optimistic concurrency to prevent double-increments.
        """
        doc = self._read_doc(pr_number)
        now = datetime.now(timezone.utc).isoformat()

        if doc is None:
            # First attempt — create document
            new_doc = {
                "id": self._doc_id(pr_number),
                "pr_number": pr_number,
                "repo": self._repo,
                "attempt_count": 1,
                "first_attempt_at": now,
                "last_attempt_at": now,
                "status": "in_progress",
            }
            self._container.create_item(new_doc)
            logger.debug("CosmosDB: created attempt doc for PR #%d", pr_number)
            return 1

        # Subsequent attempt — increment with ETag guard
        etag = doc.get("_etag")
        doc["attempt_count"] += 1
        doc["last_attempt_at"] = now

        try:
            self._container.replace_item(
                item=doc["id"],
                body=doc,
                etag=etag,
                match_condition="IfMatch",  # Optimistic concurrency
            )
        except cosmos_exc.CosmosAccessConditionFailedError:
            # Another instance updated this doc between our read and write.
            # Re-read and retry once — if it fails again, raise so the caller
            # knows the increment did not happen.
            logger.warning(
                "CosmosDB: ETag conflict on PR #%d — retrying increment once.", pr_number
            )
            doc = self._read_doc(pr_number)
            if doc is None:
                raise RuntimeError(f"Document for PR #{pr_number} disappeared during increment.")
            doc["attempt_count"] += 1
            doc["last_attempt_at"] = now
            self._container.replace_item(item=doc["id"], body=doc)

        new_count = doc["attempt_count"]
        logger.debug("CosmosDB: PR #%d attempt count is now %d", pr_number, new_count)
        return new_count

    # ── Extra metadata methods (not part of the base protocol) ───────────────

    def mark_escalated(self, pr_number: int) -> None:
        """Mark the PR as escalated (retry limit hit, needs human attention)."""
        self._update_status(pr_number, "escalated")

    def mark_resolved(self, pr_number: int) -> None:
        """Mark the PR as resolved (CI passed, no more retries needed)."""
        self._update_status(pr_number, "resolved")

    def get_all_in_progress(self) -> list:
        """
        Return all PRs in this repo that are currently in the 'in_progress' state.
        Useful for the Watcher's startup scan to find PRs it should be watching.
        """
        query = (
            "SELECT * FROM c WHERE c.repo = @repo AND c.status = 'in_progress'"
        )
        items = list(self._container.query_items(
            query=query,
            parameters=[{"name": "@repo", "value": self._repo}],
            enable_cross_partition_query=True,
        ))
        return items

    # ── Private helpers ───────────────────────────────────────────────────────

    def _doc_id(self, pr_number: int) -> str:
        # Encode repo + PR number as the document ID so it's globally unique
        # and deterministic (safe to re-create if the doc somehow gets deleted).
        return f"{self._repo}#{pr_number}"

    def _read_doc(self, pr_number: int) -> Optional[dict]:
        doc_id = self._doc_id(pr_number)
        try:
            return self._container.read_item(item=doc_id, partition_key=doc_id)
        except cosmos_exc.CosmosResourceNotFoundError:
            return None

    def _update_status(self, pr_number: int, status: str) -> None:
        doc = self._read_doc(pr_number)
        if doc is None:
            logger.warning(
                "CosmosDB: cannot set status=%s for PR #%d — document not found.",
                status, pr_number,
            )
            return
        doc["status"] = status
        doc["last_attempt_at"] = datetime.now(timezone.utc).isoformat()
        self._container.replace_item(item=doc["id"], body=doc)
        logger.info("CosmosDB: PR #%d status set to '%s'", pr_number, status)
