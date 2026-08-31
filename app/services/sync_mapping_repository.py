"""GitHub-object <-> Linear-issue mapping storage. See CLAUDE.md §1c.

Mirrors `app/services/job_repository.py`'s shape exactly — same in-memory /
SQLite split, same reasoning (in-memory is real but loses state on restart).
A separate store from `ReviewJob`, because a `SyncMapping` tracks a wholly
different relationship: not AIDA-MATE's own review lifecycle, but "this
GitHub Issue/alert became this Linear issue."
"""

import asyncio

from app.models.sync_mapping import SyncMapping


class InMemorySyncMappingRepository:
    """Process-local sync-mapping store.

    Guarded by an `asyncio.Lock` for the same reason `InMemoryReviewJobRepository`
    is: concurrent webhook deliveries for the same GitHub object must not both
    observe "no mapping" and both create one.
    """

    def __init__(self) -> None:
        self._mappings: dict[str, SyncMapping] = {}
        self._by_fingerprint: dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def find_by_fingerprint(self, fingerprint: str) -> SyncMapping | None:
        """Look up an existing mapping by its dedup fingerprint, if any."""
        mapping_id = self._by_fingerprint.get(fingerprint)
        return self._mappings.get(mapping_id) if mapping_id else None

    async def find_by_linear_issue_id(self, linear_issue_id: str) -> SyncMapping | None:
        """Look up the mapping that created/updates a given Linear issue, if any."""
        for mapping in self._mappings.values():
            if mapping.linear_issue_id == linear_issue_id:
                return mapping
        return None

    async def create(self, mapping: SyncMapping) -> tuple[SyncMapping, bool]:
        """Store a new mapping, or return the existing one for the same fingerprint.

        Returns `(mapping, created)`. Atomic under the lock, same reasoning as
        `InMemoryReviewJobRepository.create_or_get`: two concurrent deliveries
        for the same GitHub object must not both win.
        """
        async with self._lock:
            existing_id = self._by_fingerprint.get(mapping.fingerprint)
            if existing_id is not None:
                existing = self._mappings.get(existing_id)
                if existing is not None:
                    return existing, False

            self._mappings[mapping.id] = mapping
            self._by_fingerprint[mapping.fingerprint] = mapping.id
            return mapping, True

    async def save(self, mapping: SyncMapping) -> SyncMapping:
        """Persist mutations to an existing mapping."""
        self._mappings[mapping.id] = mapping
        return mapping
