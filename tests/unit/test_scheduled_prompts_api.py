"""Scheduled-prompt CRUD endpoints (CLAUDE.md §1d)."""


class FakeDashboardService:
    """Records every `sync()` call instead of touching Linear."""

    def __init__(self) -> None:
        self.synced: list[str] = []

    async def sync(self, organization_id: str) -> None:
        self.synced.append(organization_id)


_BODY = {
    "title": "Security audit",
    "prompt": "Run a general security audit of this repository.",
    "repository": "acme/api",
    "run_at_time": "09:00",
    "timezone": "Asia/Kolkata",
    "linear_issue_id": "issue-1",
    "organization_id": "org-1",
}


def test_create_scheduled_prompt(client) -> None:
    response = client.post("/scheduled-prompts", json=_BODY)

    assert response.status_code == 201
    body = response.json()
    assert body["title"] == "Security audit"
    assert body["repository"] == "acme/api"
    assert body["organization_id"] == "org-1"
    assert body["enabled"] is True
    assert body["frequency"] == "daily"
    assert body["last_run_at"] is None


def test_create_without_organization_id_is_rejected_when_ambiguous(client) -> None:
    """No installation is authorized in the default test env, so there's no
    default to fall back to — the same "don't guess" behavior as
    `LinearTokenStore.get_default()`."""
    body = {k: v for k, v in _BODY.items() if k != "organization_id"}

    response = client.post("/scheduled-prompts", json=body)

    assert response.status_code == 400


def test_create_rejects_a_malformed_run_at_time(client) -> None:
    response = client.post("/scheduled-prompts", json={**_BODY, "run_at_time": "9am"})

    assert response.status_code == 422


def test_create_rejects_an_out_of_range_time(client) -> None:
    response = client.post("/scheduled-prompts", json={**_BODY, "run_at_time": "25:00"})

    assert response.status_code == 422


def test_create_rejects_an_unknown_timezone(client) -> None:
    response = client.post("/scheduled-prompts", json={**_BODY, "timezone": "Mars/Olympus_Mons"})

    assert response.status_code == 422


def test_create_rejects_a_malformed_repository(client) -> None:
    response = client.post("/scheduled-prompts", json={**_BODY, "repository": "not-a-repo-slug"})

    assert response.status_code == 422


def test_create_rejects_a_repository_outside_the_allowlist(client) -> None:
    """Security fix: an unauthenticated caller must not be able to target an
    arbitrary repository — only `GITHUB_REPO_ALLOWLIST` entries are allowed."""
    response = client.post("/scheduled-prompts", json={**_BODY, "repository": "someone-else/private-repo"})

    assert response.status_code == 400


def test_update_rejects_a_repository_outside_the_allowlist(client) -> None:
    created = client.post("/scheduled-prompts", json=_BODY).json()

    response = client.patch(
        f"/scheduled-prompts/{created['id']}", json={"repository": "someone-else/private-repo"}
    )

    assert response.status_code == 400
    # Confirm the update was actually rejected, not silently accepted.
    assert client.get(f"/scheduled-prompts/{created['id']}").json()["repository"] == "acme/api"


# --- pr_number --------------------------------------------------------------


def test_create_accepts_a_pr_number(client) -> None:
    response = client.post("/scheduled-prompts", json={**_BODY, "pr_number": 42})

    assert response.status_code == 201
    assert response.json()["pr_number"] == 42


def test_create_without_pr_number_defaults_to_none(client) -> None:
    response = client.post("/scheduled-prompts", json=_BODY)

    assert response.status_code == 201
    assert response.json()["pr_number"] is None


def test_create_rejects_a_non_positive_pr_number(client) -> None:
    response = client.post("/scheduled-prompts", json={**_BODY, "pr_number": 0})

    assert response.status_code == 422


def test_update_can_set_a_pr_number(client) -> None:
    created = client.post("/scheduled-prompts", json=_BODY).json()

    response = client.patch(f"/scheduled-prompts/{created['id']}", json={"pr_number": 7})

    assert response.status_code == 200
    assert response.json()["pr_number"] == 7


def test_update_rejects_a_non_positive_pr_number(client) -> None:
    created = client.post("/scheduled-prompts", json=_BODY).json()

    response = client.patch(f"/scheduled-prompts/{created['id']}", json={"pr_number": -1})

    assert response.status_code == 422


# --- Frequency validation -------------------------------------------------------


def test_create_once_requires_run_on_date(client) -> None:
    body = {**_BODY, "frequency": "once"}

    response = client.post("/scheduled-prompts", json=body)

    assert response.status_code == 422


def test_create_once_succeeds_with_run_on_date(client) -> None:
    body = {**_BODY, "frequency": "once", "run_on_date": "2026-08-25"}

    response = client.post("/scheduled-prompts", json=body)

    assert response.status_code == 201
    assert response.json()["run_on_date"] == "2026-08-25"


def test_create_once_rejects_a_malformed_run_on_date(client) -> None:
    body = {**_BODY, "frequency": "once", "run_on_date": "25-08-2026"}

    response = client.post("/scheduled-prompts", json=body)

    assert response.status_code == 422


def test_create_hourly_requires_interval_hours(client) -> None:
    body = {k: v for k, v in _BODY.items() if k != "run_at_time"}
    body["frequency"] = "hourly"

    response = client.post("/scheduled-prompts", json=body)

    assert response.status_code == 422


def test_create_hourly_succeeds_with_interval_hours(client) -> None:
    body = {k: v for k, v in _BODY.items() if k != "run_at_time"}
    body["frequency"] = "hourly"
    body["interval_hours"] = 2

    response = client.post("/scheduled-prompts", json=body)

    assert response.status_code == 201
    assert response.json()["interval_hours"] == 2


def test_create_hourly_rejects_an_out_of_range_interval(client) -> None:
    body = {k: v for k, v in _BODY.items() if k != "run_at_time"}
    body["frequency"] = "hourly"
    body["interval_hours"] = 24

    response = client.post("/scheduled-prompts", json=body)

    assert response.status_code == 422


def test_create_weekly_requires_day_of_week(client) -> None:
    body = {**_BODY, "frequency": "weekly"}

    response = client.post("/scheduled-prompts", json=body)

    assert response.status_code == 422


def test_create_weekly_succeeds_with_day_of_week(client) -> None:
    body = {**_BODY, "frequency": "weekly", "day_of_week": 0}

    response = client.post("/scheduled-prompts", json=body)

    assert response.status_code == 201
    assert response.json()["day_of_week"] == 0


def test_create_weekly_rejects_an_out_of_range_day(client) -> None:
    body = {**_BODY, "frequency": "weekly", "day_of_week": 7}

    response = client.post("/scheduled-prompts", json=body)

    assert response.status_code == 422


def test_create_monthly_requires_day_of_month(client) -> None:
    body = {**_BODY, "frequency": "monthly"}

    response = client.post("/scheduled-prompts", json=body)

    assert response.status_code == 422


def test_create_monthly_succeeds_with_day_of_month(client) -> None:
    body = {**_BODY, "frequency": "monthly", "day_of_month": 15}

    response = client.post("/scheduled-prompts", json=body)

    assert response.status_code == 201
    assert response.json()["day_of_month"] == 15


def test_create_monthly_rejects_an_out_of_range_day(client) -> None:
    body = {**_BODY, "frequency": "monthly", "day_of_month": 32}

    response = client.post("/scheduled-prompts", json=body)

    assert response.status_code == 422


def test_list_scheduled_prompts(client) -> None:
    client.post("/scheduled-prompts", json=_BODY)
    client.post("/scheduled-prompts", json={**_BODY, "title": "Second"})

    response = client.get("/scheduled-prompts")

    assert response.status_code == 200
    titles = {item["title"] for item in response.json()}
    assert titles == {"Security audit", "Second"}


def test_list_when_empty_returns_an_empty_list(client) -> None:
    response = client.get("/scheduled-prompts")

    assert response.status_code == 200
    assert response.json() == []


def test_get_scheduled_prompt(client) -> None:
    created = client.post("/scheduled-prompts", json=_BODY).json()

    response = client.get(f"/scheduled-prompts/{created['id']}")

    assert response.status_code == 200
    assert response.json()["id"] == created["id"]


def test_get_unknown_id_is_404(client) -> None:
    response = client.get("/scheduled-prompts/does-not-exist")

    assert response.status_code == 404


def test_patch_updates_a_subset_of_fields(client) -> None:
    created = client.post("/scheduled-prompts", json=_BODY).json()

    response = client.patch(f"/scheduled-prompts/{created['id']}", json={"enabled": False})

    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is False
    assert body["title"] == "Security audit"  # untouched


def test_patch_unknown_id_is_404(client) -> None:
    response = client.patch("/scheduled-prompts/does-not-exist", json={"enabled": False})

    assert response.status_code == 404


def test_delete_removes_the_schedule(client) -> None:
    created = client.post("/scheduled-prompts", json=_BODY).json()

    response = client.delete(f"/scheduled-prompts/{created['id']}")

    assert response.status_code == 204
    assert client.get(f"/scheduled-prompts/{created['id']}").status_code == 404


def test_delete_unknown_id_is_still_a_204(client) -> None:
    """Deletion is idempotent — matching this codebase's other delete endpoints."""
    response = client.delete("/scheduled-prompts/does-not-exist")

    assert response.status_code == 204


# --- Dashboard sync on mutation -----------------------------------------------


def test_create_syncs_the_dashboard(client) -> None:
    dashboard = FakeDashboardService()
    client.app.state.scheduled_prompt_dashboard_service = dashboard

    client.post("/scheduled-prompts", json=_BODY)

    assert dashboard.synced == ["org-1"]


def test_update_syncs_the_dashboard(client) -> None:
    created = client.post("/scheduled-prompts", json=_BODY).json()
    dashboard = FakeDashboardService()
    client.app.state.scheduled_prompt_dashboard_service = dashboard

    client.patch(f"/scheduled-prompts/{created['id']}", json={"enabled": False})

    assert dashboard.synced == ["org-1"]


def test_delete_syncs_the_dashboard(client) -> None:
    created = client.post("/scheduled-prompts", json=_BODY).json()
    dashboard = FakeDashboardService()
    client.app.state.scheduled_prompt_dashboard_service = dashboard

    client.delete(f"/scheduled-prompts/{created['id']}")

    assert dashboard.synced == ["org-1"]


def test_delete_unknown_id_does_not_sync_the_dashboard(client) -> None:
    dashboard = FakeDashboardService()
    client.app.state.scheduled_prompt_dashboard_service = dashboard

    client.delete("/scheduled-prompts/does-not-exist")

    assert dashboard.synced == []


def test_mutations_are_fine_without_a_dashboard_service_configured(client) -> None:
    """Default test env: `scheduled_prompt_dashboard_service` is None."""
    assert client.app.state.scheduled_prompt_dashboard_service is None

    response = client.post("/scheduled-prompts", json=_BODY)

    assert response.status_code == 201
