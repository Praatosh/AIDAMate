"""Review job storage.

Phase 1 ships an in-memory implementation satisfying `IReviewJobRepository`.
It is genuinely useful — it enforces idempotency within a process — but job
state does not survive a restart. A SQL-backed implementation arrives in
Phase 13 alongside durable idempotency and retries, at which point
`DATABASE_URL` becomes load-bearing. No calling code changes.
"""

import asyncio

from app.models.common import ReviewJobStatus
from app.models.review import ReviewJob


class InMemoryReviewJobRepository:
    """Process-local review job store.

    Guarded by an `asyncio.Lock` so concurrent webhook deliveries cannot
    interleave a get-then-create and both win the idempotency check.
    """

    def __init__(self) -> None:
        self._jobs: dict[str, ReviewJob] = {}
        self._by_idempotency_key: dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def create(self, job: ReviewJob) -> ReviewJob:
        """Store a new job, or return the existing one for the same key."""
        stored, _ = await self.create_or_get(job)
        return stored

    async def create_or_get(self, job: ReviewJob) -> tuple[ReviewJob, bool]:
        """Store `job`, or return an in-flight one with the same key.

        Returns `(job, created)`.

        An existing job suppresses the new one **only while it is still
        running**. Once it reaches a terminal state the key is reused, so
        re-delegating an issue deliberately triggers a fresh review rather than
        silently returning a stale result. Duplicate deliveries that arrive
        during processing — the case worth guarding — are still collapsed.

        The whole check-and-insert is inside the lock so two concurrent
        deliveries cannot both observe "absent" and both create a job.
        """
        async with self._lock:
            existing_id = self._by_idempotency_key.get(job.idempotency_key)
            if existing_id is not None:
                existing = self._jobs.get(existing_id)
                if existing is not None and not existing.status.is_terminal:
                    return existing, False

            self._jobs[job.id] = job
            self._by_idempotency_key[job.idempotency_key] = job.id
            return job, True

    async def get(self, job_id: str) -> ReviewJob | None:
        """Fetch a job by ID."""
        return self._jobs.get(job_id)

    async def get_by_idempotency_key(self, key: str) -> ReviewJob | None:
        """Fetch a job by its idempotency key."""
        job_id = self._by_idempotency_key.get(key)
        return self._jobs.get(job_id) if job_id else None

    async def save(self, job: ReviewJob) -> ReviewJob:
        """Persist mutations to an existing job."""
        job.touch()
        self._jobs[job.id] = job
        return job

    async def list_recent(self, limit: int = 50) -> list[ReviewJob]:
        """Return the most recently created jobs, newest first."""
        ordered = sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)
        return ordered[:limit]

    async def find_by_delivery_id(self, delivery_id: str) -> ReviewJob | None:
        """Find the job created from a given webhook delivery, if any.

        Lets a redelivered webhook be dropped before it costs any GitHub calls,
        which the content-key check cannot do — that one needs the head SHA, and
        so has to fetch the PR first.
        """
        for job in self._jobs.values():
            if job.delivery_id == delivery_id:
                return job
        return None

    async def find_by_merge_confirmation_token(self, token: str) -> ReviewJob | None:
        """Find the job a merge-confirmation token belongs to, if any.

        `token is None` never matches — a job that hasn't been marked pending
        also has `merge_confirmation_token=None`, so without this guard an
        empty/missing token could accidentally resolve to an arbitrary job.
        """
        if token is None:
            return None
        for job in self._jobs.values():
            if job.merge_confirmation_token == token:
                return job
        return None

    async def recover_interrupted(self) -> list[ReviewJob]:
        """Mark jobs left mid-flight by a crash or restart as INTERRUPTED.

        In-memory storage loses everything on restart, so this never finds
        anything here. It exists because the method is part of the repository
        contract that the persistent implementation must honour, and because a
        no-op keeps the startup path identical across both backends.
        """
        return []

    async def find_completed_by_content_key(
        self, content_key: str, *, exclude: str | None = None
    ) -> ReviewJob | None:
        """Find a completed review of the same PR revision, if one exists.

        Only `COMPLETED` jobs count: a previous failure says nothing about
        whether this revision has actually been reviewed, so retrying after one
        must not be suppressed.
        """
        candidates = [
            job
            for job in self._jobs.values()
            if job.content_key == content_key
            and job.id != exclude
            and job.status is ReviewJobStatus.COMPLETED
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda job: job.created_at)

    async def find_latest_completed_by_linear_issue_id(self, linear_issue_id: str) -> ReviewJob | None:
        """Find the most recent COMPLETED review for a Linear issue, if any.

        Used by the gated auto-merge action (CLAUDE.md §1a) to find what to
        merge when an issue moves to Done — keyed by issue, unlike
        `find_completed_by_content_key`, which is keyed by PR revision.
        """
        candidates = [
            job
            for job in self._jobs.values()
            if job.linear_issue_id == linear_issue_id and job.status is ReviewJobStatus.COMPLETED
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda job: job.created_at)

    async def find_latest_completed_by_pull_request(
        self, repo_full_name: str, number: int
    ) -> ReviewJob | None:
        """Find the most recent COMPLETED review for a GitHub PR, if any.

        Used by the GitHub-merge-syncs-Linear-to-Done action (CLAUDE.md §1b) —
        the reverse lookup of `find_latest_completed_by_linear_issue_id`.
        """
        candidates = [
            job
            for job in self._jobs.values()
            if job.pull_request is not None
            and job.pull_request.repository.full_name == repo_full_name
            and job.pull_request.number == number
            and job.status is ReviewJobStatus.COMPLETED
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda job: job.created_at)
