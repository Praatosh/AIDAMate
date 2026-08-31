"""Bookkeeping for a Linear comment AIDA-MATE posted, keyed by an unguessable
URL token rather than the Linear comment id itself.

Every `LinearService.add_comment` call appends a "delete this comment" link
built from `id` (the token) to the comment body, then stores this mapping so
`app/api/comment_deletion.py`'s confirmation page can resolve the token back
to the real `linear_comment_id` a `commentDelete` mutation needs. The token,
not the comment id, is what's public — same "unguessable bearer token in a
URL" pattern as `ReviewJob`'s `review_id` in the merge-confirmation link.
"""

from datetime import UTC, datetime
from uuid import uuid4

from pydantic import BaseModel, Field


def _utcnow() -> datetime:
    """Current UTC time. Indirected so tests can patch it in one place."""
    return datetime.now(UTC)


class PostedComment(BaseModel):
    """One Linear comment AIDA-MATE posted, and how to delete it later."""

    id: str = Field(default_factory=lambda: str(uuid4()))
    linear_comment_id: str
    organization_id: str | None = None
    created_at: datetime = Field(default_factory=_utcnow)
