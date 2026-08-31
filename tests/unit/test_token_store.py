"""Installation and OAuth-state storage."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.models.linear import LinearInstallation, OAuthState
from app.services.token_store import (
    FileLinearTokenStore,
    InMemoryLinearTokenStore,
    InMemoryOAuthStateStore,
)


def _installation(org: str = "org-1", *, refresh_token: str | None = "refresh-tok") -> LinearInstallation:
    return LinearInstallation(
        organization_id=org, actor_id="app-1", access_token="tok", refresh_token=refresh_token
    )


# --- Installations ----------------------------------------------------------


async def test_save_and_get() -> None:
    store = InMemoryLinearTokenStore()
    await store.save(_installation())

    assert (await store.get("org-1")).actor_id == "app-1"


async def test_get_unknown_returns_none() -> None:
    assert await InMemoryLinearTokenStore().get("nope") is None


async def test_save_replaces_existing_installation() -> None:
    """Re-authorizing a workspace should update it, not duplicate it."""
    store = InMemoryLinearTokenStore()
    await store.save(_installation())
    await store.save(
        LinearInstallation(organization_id="org-1", actor_id="app-1", access_token="tok-2")
    )

    assert len(await store.list_all()) == 1
    assert (await store.get("org-1")).access_token.get_secret_value() == "tok-2"


async def test_default_returns_sole_installation() -> None:
    store = InMemoryLinearTokenStore()
    await store.save(_installation())

    assert (await store.get_default()).organization_id == "org-1"


async def test_default_is_none_when_ambiguous() -> None:
    store = InMemoryLinearTokenStore()
    await store.save(_installation("org-1"))
    await store.save(_installation("org-2"))

    assert await store.get_default() is None


async def test_default_is_none_when_empty() -> None:
    assert await InMemoryLinearTokenStore().get_default() is None


async def test_delete_reports_whether_present() -> None:
    store = InMemoryLinearTokenStore()
    await store.save(_installation())

    assert await store.delete("org-1") is True
    assert await store.delete("org-1") is False


# --- File-backed installations -----------------------------------------------
#
# `FileLinearTokenStore` satisfies the same interface exercised above; these
# add only what's specific to persistence — surviving a fresh instance
# (simulating a server restart), tolerating a missing/corrupt file, and
# actually round-tripping the real secret rather than persisting the mask.


async def test_file_store_persists_across_a_fresh_instance(tmp_path: Path) -> None:
    """The scenario this exists for: a server restart must not lose the install."""
    path = tmp_path / "tokens.json"
    await FileLinearTokenStore(path).save(_installation())

    reloaded = FileLinearTokenStore(path)

    assert (await reloaded.get("org-1")).actor_id == "app-1"


async def test_file_store_round_trips_the_real_secret_not_the_mask(tmp_path: Path) -> None:
    path = tmp_path / "tokens.json"
    await FileLinearTokenStore(path).save(_installation())

    on_disk = json.loads(path.read_text())
    assert on_disk["org-1"]["access_token"] == "tok"  # not "**********"

    reloaded = await FileLinearTokenStore(path).get("org-1")
    assert reloaded.access_token.get_secret_value() == "tok"
    assert reloaded.refresh_token.get_secret_value() == "refresh-tok"


async def test_file_store_handles_a_missing_refresh_token(tmp_path: Path) -> None:
    path = tmp_path / "tokens.json"
    await FileLinearTokenStore(path).save(_installation(refresh_token=None))

    reloaded = await FileLinearTokenStore(path).get("org-1")

    assert reloaded.refresh_token is None


async def test_file_store_starts_empty_when_the_file_does_not_exist(tmp_path: Path) -> None:
    store = FileLinearTokenStore(tmp_path / "does-not-exist.json")

    assert await store.list_all() == []


async def test_file_store_starts_empty_on_corrupt_json(tmp_path: Path) -> None:
    path = tmp_path / "tokens.json"
    path.write_text("{not valid json", encoding="utf-8")

    store = FileLinearTokenStore(path)

    assert await store.list_all() == []


async def test_file_store_skips_one_unreadable_entry_but_keeps_the_rest(tmp_path: Path) -> None:
    path = tmp_path / "tokens.json"
    await FileLinearTokenStore(path).save(_installation("org-1"))

    on_disk = json.loads(path.read_text())
    on_disk["org-broken"] = {"organization_id": "org-broken"}  # missing required fields
    path.write_text(json.dumps(on_disk), encoding="utf-8")

    store = FileLinearTokenStore(path)

    assert await store.get("org-broken") is None
    assert (await store.get("org-1")).actor_id == "app-1"


async def test_file_store_creates_parent_directories(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "dir" / "tokens.json"

    await FileLinearTokenStore(path).save(_installation())

    assert path.exists()


async def test_file_store_replaces_existing_installation(tmp_path: Path) -> None:
    path = tmp_path / "tokens.json"
    store = FileLinearTokenStore(path)
    await store.save(_installation())
    await store.save(
        LinearInstallation(organization_id="org-1", actor_id="app-1", access_token="tok-2")
    )

    assert len(await store.list_all()) == 1
    assert (await store.get("org-1")).access_token.get_secret_value() == "tok-2"


async def test_file_store_delete_persists(tmp_path: Path) -> None:
    path = tmp_path / "tokens.json"
    store = FileLinearTokenStore(path)
    await store.save(_installation())

    assert await store.delete("org-1") is True

    reloaded = FileLinearTokenStore(path)
    assert await reloaded.list_all() == []


# --- OAuth state ------------------------------------------------------------


async def test_state_roundtrip() -> None:
    store = InMemoryOAuthStateStore()
    await store.put(OAuthState(state="s1", code_verifier="v1"))

    consumed = await store.consume("s1")

    assert consumed is not None
    assert consumed.code_verifier == "v1"


async def test_state_is_single_use() -> None:
    """Consuming twice must fail, or a captured callback could be replayed."""
    store = InMemoryOAuthStateStore()
    await store.put(OAuthState(state="s1", code_verifier="v1"))

    assert await store.consume("s1") is not None
    assert await store.consume("s1") is None


async def test_unknown_state_returns_none() -> None:
    assert await InMemoryOAuthStateStore().consume("never-issued") is None


async def test_expired_state_is_rejected() -> None:
    store = InMemoryOAuthStateStore(ttl=timedelta(minutes=10))
    await store.put(
        OAuthState(
            state="old", code_verifier="v", created_at=datetime.now(UTC) - timedelta(minutes=11)
        )
    )

    assert await store.consume("old") is None


async def test_expired_states_are_swept_on_write() -> None:
    """Abandoned installs must not grow the map without bound."""
    store = InMemoryOAuthStateStore(ttl=timedelta(minutes=10))
    for i in range(5):
        await store.put(
            OAuthState(
                state=f"old-{i}",
                code_verifier="v",
                created_at=datetime.now(UTC) - timedelta(minutes=11),
            )
        )
    await store.put(OAuthState(state="fresh", code_verifier="v"))

    assert len(store._states) == 1
    assert await store.consume("fresh") is not None
