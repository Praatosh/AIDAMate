"""Storage for Linear OAuth installations and pending authorization state.

`InMemoryLinearTokenStore` is in-memory. That is genuinely correct for
authorization *state* (`InMemoryOAuthStateStore`) — those records are
single-use and expire in minutes, so losing them on restart costs at most one
retried install.

It is a real limitation for *installations*: a restart forces every workspace
to reconnect. `FileLinearTokenStore` closes that gap for local development —
see its own docstring for what it is and is not suitable for. A database-
backed store with encryption at rest remains the production path (Phase 13),
behind the same interface both classes here already implement.
"""

import asyncio
import json
from datetime import timedelta
from pathlib import Path

from pydantic import ValidationError

from app.core.logging import get_logger
from app.models.linear import LinearInstallation, OAuthState

logger = get_logger(__name__)


class InMemoryLinearTokenStore:
    """Process-local store of authorized Linear workspaces, keyed by organization."""

    def __init__(self) -> None:
        self._installations: dict[str, LinearInstallation] = {}
        self._lock = asyncio.Lock()

    async def save(self, installation: LinearInstallation) -> LinearInstallation:
        """Insert or replace the installation for its organization."""
        async with self._lock:
            self._installations[installation.organization_id] = installation
            return installation

    async def get(self, organization_id: str) -> LinearInstallation | None:
        """Fetch the installation for an organization, if authorized."""
        return self._installations.get(organization_id)

    async def get_default(self) -> LinearInstallation | None:
        """Return the sole installation when exactly one exists.

        Convenience for single-workspace deployments and for webhook payloads
        that arrive without an organization ID. Returns None when ambiguous,
        rather than guessing which workspace was meant.
        """
        if len(self._installations) == 1:
            return next(iter(self._installations.values()))
        return None

    async def list_all(self) -> list[LinearInstallation]:
        """All authorized installations."""
        return list(self._installations.values())

    async def delete(self, organization_id: str) -> bool:
        """Remove an installation. Returns whether one was present."""
        async with self._lock:
            return self._installations.pop(organization_id, None) is not None


def _installation_to_dict(installation: LinearInstallation) -> dict:
    """Serialize an installation to plain JSON, unmasking the `SecretStr` fields.

    `model_dump()` renders `SecretStr` as `**********` by default — the right
    behavior for logs and API responses, but wrong here: persisting the mask
    instead of the real token would make the file useless to reload from.
    """
    data = installation.model_dump(mode="json")
    data["access_token"] = installation.access_token.get_secret_value()
    data["refresh_token"] = (
        installation.refresh_token.get_secret_value() if installation.refresh_token else None
    )
    return data


class FileLinearTokenStore:
    """Persists Linear OAuth installations to a local JSON file.

    A local-development convenience so a server restart does not force every
    workspace to reconnect — not a production credential store. Two things
    that distinguishes it from a real one:

    * **Not encrypted at rest.** The file holds real access/refresh tokens in
      plain JSON. Keep it out of version control (alongside `.env`) and off
      any shared or multi-user host.
    * **Not safe for multiple processes.** Locking is `asyncio.Lock`, which
      only serializes access within this one process. Fine for a single dev
      server; wrong for anything horizontally scaled.

    Same interface as `InMemoryLinearTokenStore`, so callers never need to
    know which backend is active. Writes are atomic (write to a temp file,
    then replace) so a crash mid-write cannot corrupt the store.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = asyncio.Lock()
        self._installations: dict[str, LinearInstallation] = self._load()

    def _load(self) -> dict[str, LinearInstallation]:
        if not self._path.exists():
            return {}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "Could not read the Linear token store file; starting empty",
                extra={"path": str(self._path), "error": str(exc)},
            )
            return {}

        installations: dict[str, LinearInstallation] = {}
        for organization_id, data in raw.items():
            try:
                installations[organization_id] = LinearInstallation.model_validate(data)
            except ValidationError as exc:
                logger.warning(
                    "Dropping an unreadable entry from the Linear token store",
                    extra={"organization_id": organization_id, "error": str(exc)},
                )
        return installations

    def _write(self) -> None:
        payload = {
            organization_id: _installation_to_dict(installation)
            for organization_id, installation in self._installations.items()
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._path.with_suffix(f"{self._path.suffix}.tmp")
        tmp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp_path.replace(self._path)

    async def save(self, installation: LinearInstallation) -> LinearInstallation:
        """Insert or replace the installation for its organization."""
        async with self._lock:
            self._installations[installation.organization_id] = installation
            self._write()
            return installation

    async def get(self, organization_id: str) -> LinearInstallation | None:
        """Fetch the installation for an organization, if authorized."""
        return self._installations.get(organization_id)

    async def get_default(self) -> LinearInstallation | None:
        """Return the sole installation when exactly one exists.

        Convenience for single-workspace deployments and for webhook payloads
        that arrive without an organization ID. Returns None when ambiguous,
        rather than guessing which workspace was meant.
        """
        if len(self._installations) == 1:
            return next(iter(self._installations.values()))
        return None

    async def list_all(self) -> list[LinearInstallation]:
        """All authorized installations."""
        return list(self._installations.values())

    async def delete(self, organization_id: str) -> bool:
        """Remove an installation. Returns whether one was present."""
        async with self._lock:
            removed = self._installations.pop(organization_id, None) is not None
            if removed:
                self._write()
            return removed


class InMemoryOAuthStateStore:
    """Pending OAuth authorization attempts.

    `consume` is deliberately single-use: replaying a captured callback URL must
    not mint a second token. Expired entries are swept on each write so a stream
    of abandoned installs cannot grow the map without bound.
    """

    def __init__(self, ttl: timedelta = timedelta(minutes=10)) -> None:
        self._states: dict[str, OAuthState] = {}
        self._ttl = ttl
        self._lock = asyncio.Lock()

    async def put(self, state: OAuthState) -> None:
        """Record a pending authorization, sweeping expired entries first."""
        async with self._lock:
            self._purge_expired()
            self._states[state.state] = state

    async def consume(self, state_value: str) -> OAuthState | None:
        """Atomically fetch and remove a pending authorization.

        Returns None if unknown, already used, or expired — the caller must
        treat all three identically and reject the callback.
        """
        async with self._lock:
            self._purge_expired()
            record = self._states.pop(state_value, None)
            if record is None or record.is_expired(self._ttl):
                return None
            return record

    def _purge_expired(self) -> None:
        """Drop expired entries. Caller must hold the lock."""
        expired = [key for key, value in self._states.items() if value.is_expired(self._ttl)]
        for key in expired:
            del self._states[key]
