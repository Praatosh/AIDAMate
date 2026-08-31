"""Durable review job storage, backed by SQLite.

Satisfies the same `IReviewJobRepository` contract as
`InMemoryReviewJobRepository`, so nothing above this layer knows which is
active. Two things become possible only here:

* **Review state survives a restart.** A job interrupted mid-review is
  recoverable (`recover_interrupted`) instead of vanishing, and the
  `repo + PR -> Linear issue` mapping needed to publish results outlives the
  process that learned it.
* **Dedup is enforced by the database, not by a read-then-write.** A UNIQUE
  index on `idempotency_key` means two concurrent deliveries racing to create
  the same job cannot both win: the loser gets an `IntegrityError` and reads
  back the winner's row. A `SELECT`-then-`INSERT` in application code has a
  gap between the two statements that no amount of care closes.

The uniqueness index is **partial** — scoped to non-terminal statuses only
(`WHERE status NOT IN (...)`). Without that qualifier, re-delegating an issue
whose previous review already completed would collide with that old row
forever; the only way to accept the new job would be to delete the old one,
which is exactly the review-history loss this store exists to prevent. A
completed, skipped, failed, or interrupted job keeps its row permanently;
uniqueness only ever blocks a *second concurrently-active* job for the same
key.

Uses stdlib `sqlite3` on a worker thread rather than an async driver. The
project already reaches for `asyncio.to_thread` in both sandbox backends for
exactly this reason, and it keeps the dependency list unchanged.

Storage shape: the queryable lifecycle fields are real columns (so dedup and
recovery are index lookups), while the rest of the job round-trips through one
JSON `payload` column. That avoids a migration every time a field is added to
`ReviewJob`, at the cost of not being able to query those fields in SQL — a
trade worth making while the schema is still moving.

Not encrypted at rest, and no secret is ever written here: `ReviewJob` holds
identifiers, status, and results, never credentials.
"""

import asyncio
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from app.core.logging import get_logger
from app.models.common import ReviewJobStatus
from app.models.review import ReviewJob

logger = get_logger(__name__)

#: Rendered into a `WHERE status NOT IN (...)` clause below. Built from the
#: enum rather than hand-copied, so a future status is automatically covered
#: without this file needing to change.
_TERMINAL_STATUSES_SQL = ", ".join(f"'{status.value}'" for status in ReviewJobStatus if status.is_terminal)

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS review_jobs (
    id               TEXT PRIMARY KEY,
    idempotency_key  TEXT NOT NULL,
    delivery_id      TEXT,
    content_key      TEXT,
    status           TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    payload          TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_review_jobs_idempotency_active
    ON review_jobs (idempotency_key)
    WHERE status NOT IN ({_TERMINAL_STATUSES_SQL});
CREATE INDEX IF NOT EXISTS idx_review_jobs_delivery ON review_jobs (delivery_id);
CREATE INDEX IF NOT EXISTS idx_review_jobs_content  ON review_jobs (content_key, status);
CREATE INDEX IF NOT EXISTS idx_review_jobs_status   ON review_jobs (status);
"""


class SqliteReviewJobRepository:
    """Review job store persisted to a local SQLite file."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Open a short-lived connection, guaranteed closed on exit.

        Per-operation rather than shared: `asyncio.to_thread` may run any given
        call on any worker thread, and a SQLite connection is not safe to move
        between threads. Opening is cheap for a local file.

        A plain `with self._connect() as conn:` at the call site only got half
        of what it looked like it was getting: `sqlite3.Connection.__exit__`
        commits or rolls back the transaction, but never closes the
        connection (that's documented sqlite3 behavior, not a bug in this
        file) — every call site here used to leak a connection object,
        relying on CPython's refcounting to eventually close it via `__del__`
        rather than closing it explicitly. Security/reliability-audit finding:
        making `_connect` itself a context manager fixes every call site at
        once without changing any of them — `with self._connect() as conn:`
        still reads the same, but now the inner `with conn:` handles the
        commit/rollback and the `finally` guarantees the close.
        """
        conn = sqlite3.connect(self._path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    # -- Writes ---------------------------------------------------------------

    def _insert_sync(self, job: ReviewJob) -> tuple[ReviewJob, bool]:
        """Insert `job`, or return the still-active row blocking it.

        The partial unique index (see module docstring) only ever rejects an
        insert when a *non-terminal* job already holds this idempotency key —
        a completed, skipped, failed, or interrupted job never blocks a new
        one, so there is no case here that needs to delete or overwrite a
        finished review to make room.
        """
        with self._connect() as conn:
            try:
                conn.execute(
                    "INSERT INTO review_jobs "
                    "(id, idempotency_key, delivery_id, content_key, status, created_at, payload) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        job.id,
                        job.idempotency_key,
                        job.delivery_id,
                        job.content_key,
                        job.status.value,
                        job.created_at.isoformat(),
                        job.model_dump_json(),
                    ),
                )
                return job, True
            except sqlite3.IntegrityError:
                # Lost the race: some other job is the active holder of this
                # key. Only a non-terminal row can be the cause (see above),
                # so that is the only row worth looking for here.
                row = conn.execute(
                    f"SELECT payload FROM review_jobs WHERE idempotency_key = ? "
                    f"AND status NOT IN ({_TERMINAL_STATUSES_SQL})",
                    (job.idempotency_key,),
                ).fetchone()

        if row is None:  # pragma: no cover - only reachable if the row vanished mid-race
            return job, True
        return ReviewJob.model_validate_json(row["payload"]), False

    async def create(self, job: ReviewJob) -> ReviewJob:
        """Store a new job, or return the existing one for the same key."""
        stored, _ = await self.create_or_get(job)
        return stored

    async def create_or_get(self, job: ReviewJob) -> tuple[ReviewJob, bool]:
        """Store `job`, or return a still-running one with the same key.

        Returns `(job, created)`. Atomicity comes from the UNIQUE index, so
        concurrent callers cannot both create.
        """
        return await asyncio.to_thread(self._insert_sync, job)

    async def save(self, job: ReviewJob) -> ReviewJob:
        """Persist mutations to an existing job."""
        job.touch()

        def _save() -> None:
            with self._connect() as conn:
                conn.execute(
                    "UPDATE review_jobs SET delivery_id = ?, content_key = ?, status = ?, "
                    "payload = ? WHERE id = ?",
                    (job.delivery_id, job.content_key, job.status.value, job.model_dump_json(), job.id),
                )

        await asyncio.to_thread(_save)
        return job

    # -- Reads ----------------------------------------------------------------

    async def _fetch_one(self, sql: str, params: tuple) -> ReviewJob | None:
        def _query() -> sqlite3.Row | None:
            with self._connect() as conn:
                return conn.execute(sql, params).fetchone()

        row = await asyncio.to_thread(_query)
        return ReviewJob.model_validate_json(row["payload"]) if row else None

    async def get(self, job_id: str) -> ReviewJob | None:
        """Fetch a job by ID."""
        return await self._fetch_one("SELECT payload FROM review_jobs WHERE id = ?", (job_id,))

    async def get_by_idempotency_key(self, key: str) -> ReviewJob | None:
        """Fetch the most recent job for an idempotency key.

        Multiple rows can share a key over time — each past review keeps its
        own row (see the module docstring) — so this is "the current one",
        newest first, not "the only one".
        """
        return await self._fetch_one(
            "SELECT payload FROM review_jobs WHERE idempotency_key = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (key,),
        )

    async def find_by_delivery_id(self, delivery_id: str) -> ReviewJob | None:
        """Find the job created from a given webhook delivery, if any."""
        return await self._fetch_one(
            "SELECT payload FROM review_jobs WHERE delivery_id = ? ORDER BY created_at LIMIT 1",
            (delivery_id,),
        )

    async def find_by_merge_confirmation_token(self, token: str) -> ReviewJob | None:
        """Find the job a merge-confirmation token belongs to, if any.

        `merge_confirmation_token` isn't an indexed column (lives inside the
        JSON payload blob) — same scan-then-filter tradeoff as
        `find_latest_completed_by_linear_issue_id` below. Only a COMPLETED job
        ever has one set (`mark_merge_pending` is only called on those), so
        that's still a useful pre-filter.

        `token is None` never matches — a job that hasn't been marked pending
        also has `merge_confirmation_token=None`, so without this guard an
        empty/missing token could accidentally resolve to an arbitrary job.
        """
        if token is None:
            return None

        def _query() -> list[sqlite3.Row]:
            with self._connect() as conn:
                return conn.execute(
                    "SELECT payload FROM review_jobs WHERE status = ? ORDER BY created_at DESC",
                    (ReviewJobStatus.COMPLETED.value,),
                ).fetchall()

        rows = await asyncio.to_thread(_query)
        for row in rows:
            job = ReviewJob.model_validate_json(row["payload"])
            if job.merge_confirmation_token == token:
                return job
        return None

    async def find_completed_by_content_key(
        self, content_key: str, *, exclude: str | None = None
    ) -> ReviewJob | None:
        """Find a completed review of the same PR revision, if one exists.

        Only `COMPLETED` counts. A previous failure says nothing about whether
        the revision was actually reviewed, so retrying after one must not be
        suppressed — and a previous `SKIPPED` would chain skips forever.
        """
        return await self._fetch_one(
            "SELECT payload FROM review_jobs WHERE content_key = ? AND status = ? "
            "AND id IS NOT ? ORDER BY created_at DESC LIMIT 1",
            (content_key, ReviewJobStatus.COMPLETED.value, exclude),
        )

    async def find_latest_completed_by_linear_issue_id(self, linear_issue_id: str) -> ReviewJob | None:
        """Find the most recent COMPLETED review for a Linear issue, if any.

        `linear_issue_id` isn't an indexed column (it lives inside the JSON
        `payload` blob — see the module docstring's documented tradeoff), so
        this scans COMPLETED rows newest-first and filters in Python. Fine at
        this project's scale; a dedicated indexed column would be the fix if
        that ever changes.
        """

        def _query() -> list[sqlite3.Row]:
            with self._connect() as conn:
                return conn.execute(
                    "SELECT payload FROM review_jobs WHERE status = ? ORDER BY created_at DESC",
                    (ReviewJobStatus.COMPLETED.value,),
                ).fetchall()

        rows = await asyncio.to_thread(_query)
        for row in rows:
            job = ReviewJob.model_validate_json(row["payload"])
            if job.linear_issue_id == linear_issue_id:
                return job
        return None

    async def find_latest_completed_by_pull_request(
        self, repo_full_name: str, number: int
    ) -> ReviewJob | None:
        """Find the most recent COMPLETED review for a GitHub PR, if any.

        The reverse lookup of `find_latest_completed_by_linear_issue_id`,
        used by the GitHub-merge-syncs-Linear-to-Done action (CLAUDE.md §1b).
        Same scan-then-filter tradeoff: neither the PR's repo nor its number
        is an indexed column.
        """

        def _query() -> list[sqlite3.Row]:
            with self._connect() as conn:
                return conn.execute(
                    "SELECT payload FROM review_jobs WHERE status = ? ORDER BY created_at DESC",
                    (ReviewJobStatus.COMPLETED.value,),
                ).fetchall()

        rows = await asyncio.to_thread(_query)
        for row in rows:
            job = ReviewJob.model_validate_json(row["payload"])
            if (
                job.pull_request is not None
                and job.pull_request.repository.full_name == repo_full_name
                and job.pull_request.number == number
            ):
                return job
        return None

    async def list_recent(self, limit: int = 50) -> list[ReviewJob]:
        """Return the most recently created jobs, newest first."""

        def _query() -> list[sqlite3.Row]:
            with self._connect() as conn:
                return conn.execute(
                    "SELECT payload FROM review_jobs ORDER BY created_at DESC LIMIT ?", (limit,)
                ).fetchall()

        rows = await asyncio.to_thread(_query)
        return [ReviewJob.model_validate_json(row["payload"]) for row in rows]

    # -- Recovery -------------------------------------------------------------

    async def recover_interrupted(self) -> list[ReviewJob]:
        """Mark jobs left mid-flight by a crash or restart as INTERRUPTED.

        Without this, a process that dies during `ANALYZING` leaves a row that
        is neither running nor finished — invisible to the retry path, which
        only accepts terminal-but-retryable states, and permanently stuck.

        Returns the recovered jobs so startup can report how many there were.
        """
        non_terminal = [status.value for status in ReviewJobStatus if not status.is_terminal]

        def _query() -> list[sqlite3.Row]:
            placeholders = ",".join("?" * len(non_terminal))
            with self._connect() as conn:
                return conn.execute(
                    f"SELECT payload FROM review_jobs WHERE status IN ({placeholders})",
                    tuple(non_terminal),
                ).fetchall()

        rows = await asyncio.to_thread(_query)
        recovered: list[ReviewJob] = []
        for row in rows:
            job = ReviewJob.model_validate_json(row["payload"])
            job.mark_interrupted()
            await self.save(job)
            recovered.append(job)

        if recovered:
            logger.warning(
                "Recovered review jobs interrupted by a restart",
                extra={"count": len(recovered), "review_ids": [job.id for job in recovered]},
            )
        return recovered
