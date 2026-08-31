"""Scheduled-prompt storage. See CLAUDE.md §1d.

Mirrors `app/services/sync_mapping_repository.py`'s shape exactly — same
in-memory/SQLite split, same reasoning (in-memory is real but loses state on
restart). `list_all()` is this store's one addition over that template: the
scheduler worker has to enumerate every entry each tick, where the sync-
mapping store only ever needed point lookups.
"""

import asyncio

from app.models.scheduled_prompt import ScheduledPrompt


class InMemoryScheduledPromptRepository:
    """Process-local scheduled-prompt store."""

    def __init__(self) -> None:
        self._prompts: dict[str, ScheduledPrompt] = {}
        self._lock = asyncio.Lock()

    async def create(self, scheduled: ScheduledPrompt) -> ScheduledPrompt:
        async with self._lock:
            self._prompts[scheduled.id] = scheduled
            return scheduled

    async def save(self, scheduled: ScheduledPrompt) -> ScheduledPrompt:
        """Persist mutations to an existing schedule."""
        self._prompts[scheduled.id] = scheduled
        return scheduled

    async def get(self, scheduled_id: str) -> ScheduledPrompt | None:
        return self._prompts.get(scheduled_id)

    async def list_all(self) -> list[ScheduledPrompt]:
        return list(self._prompts.values())

    async def delete(self, scheduled_id: str) -> None:
        self._prompts.pop(scheduled_id, None)
