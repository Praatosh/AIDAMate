"""Scheduled-prompt CRUD endpoints. See CLAUDE.md §1d.

Same conventions as `app/api/reviews.py`: services reached via
`request.app.state.*`, a response model with an `.of()` projector. The
scheduler worker itself (`app/workers/scheduled_prompt_worker.py`) only ever
reads through `IScheduledPromptRepository.list_all()` — these routes are the
only way a schedule is created, changed, or removed.
"""

import re
from datetime import date
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, field_validator, model_validator

from app.core.api_auth import require_management_api_key
from app.core.logging import get_logger
from app.models.scheduled_prompt import ScheduledPrompt

logger = get_logger(__name__)

# Requires a valid X-Api-Key on every route below — see app/core/api_auth.py
# for why this router in particular needed it (security-audit finding). Only
# applies to requests that actually go through this router's own HTTP
# routing: `scheduled_prompt_form.py` calls `create_scheduled_prompt`/
# `delete_scheduled_prompt` directly as plain functions, which never runs
# this dependency — the human-facing web form deliberately keeps its own,
# separate trust model (a person clicking through a browser).
router = APIRouter(
    prefix="/scheduled-prompts",
    tags=["scheduled-prompts"],
    dependencies=[Depends(require_management_api_key)],
)

_TIME_PATTERN = re.compile(r"^\d{2}:\d{2}$")


def _validate_run_at_time(value: str) -> str:
    if not _TIME_PATTERN.match(value):
        raise ValueError("run_at_time must be 24h 'HH:MM'")
    hour, minute = (int(part) for part in value.split(":"))
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise ValueError("run_at_time must be a valid 24h time")
    return value


def _validate_timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except Exception as exc:
        raise ValueError(f"'{value}' is not a known IANA timezone name") from exc
    return value


def _validate_repository(value: str) -> str:
    cleaned = value.strip().strip("/")
    if cleaned.count("/") != 1 or not all(cleaned.split("/")):
        raise ValueError("repository must be 'owner/repo'")
    return cleaned


def _validate_run_on_date(value: str) -> str:
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("run_on_date must be an ISO 'YYYY-MM-DD' date") from exc
    return value


def _validate_pr_number(value: int) -> int:
    if value <= 0:
        raise ValueError("pr_number must be a positive integer")
    return value


_Frequency = Literal["once", "hourly", "daily", "weekly", "monthly"]


class ScheduledPromptCreate(BaseModel):
    """Body for `POST /scheduled-prompts`.

    Which of `run_on_date`/`interval_hours`/`day_of_week`/`day_of_month`/
    `run_at_time` are required depends on `frequency` — enforced by
    `_validate_frequency_fields` below, not by the individual field types
    (each stays optional so a client can omit whatever its chosen frequency
    doesn't need).
    """

    title: str
    prompt: str
    repository: str
    branch: str | None = None
    pr_number: int | None = None  # if set, wins over `branch`/the repo's default branch
    frequency: _Frequency = "daily"
    run_on_date: str | None = None
    interval_hours: int | None = None
    day_of_week: int | None = None
    day_of_month: int | None = None
    run_at_time: str | None = None
    timezone: str
    linear_issue_id: str
    organization_id: str | None = None
    enabled: bool = True

    _validate_run_at_time = field_validator("run_at_time")(
        lambda v: _validate_run_at_time(v) if v is not None else v
    )
    _validate_run_on_date = field_validator("run_on_date")(
        lambda v: _validate_run_on_date(v) if v is not None else v
    )
    _validate_timezone = field_validator("timezone")(_validate_timezone)
    _validate_repository = field_validator("repository")(_validate_repository)
    _validate_pr_number = field_validator("pr_number")(
        lambda v: _validate_pr_number(v) if v is not None else v
    )

    @model_validator(mode="after")
    def _validate_frequency_fields(self) -> "ScheduledPromptCreate":
        """Cross-field requirements per `frequency` — see CLAUDE.md §1d."""
        if self.frequency == "once":
            if self.run_on_date is None:
                raise ValueError("run_on_date is required for 'once' frequency")
            if self.run_at_time is None:
                raise ValueError("run_at_time is required for 'once' frequency")
        elif self.frequency == "hourly":
            if self.interval_hours is None or not (1 <= self.interval_hours <= 23):
                raise ValueError("interval_hours (1-23) is required for 'hourly' frequency")
        else:
            if self.run_at_time is None:
                raise ValueError(f"run_at_time is required for '{self.frequency}' frequency")
            if self.frequency == "weekly" and (
                self.day_of_week is None or not (0 <= self.day_of_week <= 6)
            ):
                raise ValueError("day_of_week (0-6) is required for 'weekly' frequency")
            if self.frequency == "monthly" and (
                self.day_of_month is None or not (1 <= self.day_of_month <= 31)
            ):
                raise ValueError("day_of_month (1-31) is required for 'monthly' frequency")
        return self


class ScheduledPromptUpdate(BaseModel):
    """Body for `PATCH /scheduled-prompts/{id}` — any subset of the mutable fields.

    Unlike `ScheduledPromptCreate`, this does not cross-validate `frequency`
    against its companion fields — a partial update legitimately touches
    only one field at a time, and this endpoint has never fully revalidated
    the resulting object. A PATCH that leaves an inconsistent combination
    (e.g. `frequency: weekly` with no `day_of_week` ever set) is an accepted
    gap, not solved here.
    """

    title: str | None = None
    prompt: str | None = None
    repository: str | None = None
    branch: str | None = None
    pr_number: int | None = None
    frequency: _Frequency | None = None
    run_on_date: str | None = None
    interval_hours: int | None = None
    day_of_week: int | None = None
    day_of_month: int | None = None
    run_at_time: str | None = None
    timezone: str | None = None
    linear_issue_id: str | None = None
    enabled: bool | None = None

    _validate_run_at_time = field_validator("run_at_time")(
        lambda v: _validate_run_at_time(v) if v is not None else v
    )
    _validate_run_on_date = field_validator("run_on_date")(
        lambda v: _validate_run_on_date(v) if v is not None else v
    )
    _validate_timezone = field_validator("timezone")(lambda v: _validate_timezone(v) if v is not None else v)
    _validate_repository = field_validator("repository")(
        lambda v: _validate_repository(v) if v is not None else v
    )
    _validate_pr_number = field_validator("pr_number")(
        lambda v: _validate_pr_number(v) if v is not None else v
    )


class ScheduledPromptResponse(BaseModel):
    """One scheduled prompt, as reported over HTTP."""

    id: str
    title: str
    prompt: str
    repository: str
    branch: str | None
    pr_number: int | None
    frequency: str
    run_on_date: str | None
    interval_hours: int | None
    day_of_week: int | None
    day_of_month: int | None
    run_at_time: str | None
    timezone: str
    linear_issue_id: str
    organization_id: str | None
    enabled: bool
    last_run_at: str | None
    created_at: str
    updated_at: str

    @classmethod
    def of(cls, scheduled: ScheduledPrompt) -> "ScheduledPromptResponse":
        return cls(
            id=scheduled.id,
            title=scheduled.title,
            prompt=scheduled.prompt,
            repository=scheduled.repository,
            branch=scheduled.branch,
            pr_number=scheduled.pr_number,
            frequency=scheduled.frequency,
            run_on_date=scheduled.run_on_date,
            interval_hours=scheduled.interval_hours,
            day_of_week=scheduled.day_of_week,
            day_of_month=scheduled.day_of_month,
            run_at_time=scheduled.run_at_time,
            timezone=scheduled.timezone,
            linear_issue_id=scheduled.linear_issue_id,
            organization_id=scheduled.organization_id,
            enabled=scheduled.enabled,
            last_run_at=scheduled.last_run_at.isoformat() if scheduled.last_run_at else None,
            created_at=scheduled.created_at.isoformat(),
            updated_at=scheduled.updated_at.isoformat(),
        )


async def _sync_dashboard(request: Request, organization_id: str) -> None:
    """Push the dashboard update for `organization_id`, if the dashboard
    service is configured — mirrors the `is not None` guard every other
    optional service in `app.state` uses at its call sites."""
    service = getattr(request.app.state, "scheduled_prompt_dashboard_service", None)
    if service is not None:
        await service.sync(organization_id)


async def _resolve_organization_id(request: Request, organization_id: str | None) -> str | None:
    """Resolve which Linear workspace a schedule without an explicit one belongs to.

    Mirrors `linear_webhook.py`'s `_resolve_actor_id`: the sole installation
    when exactly one is authorized, otherwise ambiguous. Unlike that webhook
    path (which falls back to a settings override and proceeds regardless),
    an ambiguous creation request is rejected outright — there's no later
    stage that could recover from posting to the wrong workspace.
    """
    if organization_id is not None:
        return organization_id
    store = getattr(request.app.state, "linear_token_store", None)
    if store is None:
        return None
    installation = await store.get_default()
    return installation.organization_id if installation is not None else None


def _ensure_repository_allowed(request: Request, repository: str) -> None:
    """Reject a repository outside `GITHUB_REPO_ALLOWLIST`.

    Security-audit finding, fixed here: a scheduled prompt previously had no
    restriction on which repository it could target — this endpoint is
    unauthenticated, and anything the server's GitHub credentials could reach
    was reachable through it, with results posted to an equally
    attacker-suppliable `linear_issue_id`. Matches the same allowlist check
    `app/api/github_webhook.py` already enforces for inbound GitHub events —
    an empty allowlist (the default) means nothing is allowed, fail-closed.
    """
    settings = request.app.state.settings
    if repository.lower() not in settings.github_repos:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Repository '{repository}' is not in GITHUB_REPO_ALLOWLIST.",
        )


@router.post("", response_model=ScheduledPromptResponse, status_code=status.HTTP_201_CREATED)
async def create_scheduled_prompt(request: Request, body: ScheduledPromptCreate) -> ScheduledPromptResponse:
    """Create a new scheduled prompt."""
    _ensure_repository_allowed(request, body.repository)
    organization_id = await _resolve_organization_id(request, body.organization_id)
    if organization_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="organization_id was not given and could not be resolved to a single Linear "
            "workspace; more than one workspace is authorized, so it must be specified explicitly.",
        )

    scheduled = ScheduledPrompt(
        title=body.title,
        prompt=body.prompt,
        repository=body.repository,
        branch=body.branch,
        pr_number=body.pr_number,
        frequency=body.frequency,
        run_on_date=body.run_on_date,
        interval_hours=body.interval_hours,
        day_of_week=body.day_of_week,
        day_of_month=body.day_of_month,
        run_at_time=body.run_at_time,
        timezone=body.timezone,
        linear_issue_id=body.linear_issue_id,
        organization_id=organization_id,
        enabled=body.enabled,
    )
    created = await request.app.state.scheduled_prompt_repository.create(scheduled)
    await _sync_dashboard(request, organization_id)
    return ScheduledPromptResponse.of(created)


@router.get("", response_model=list[ScheduledPromptResponse])
async def list_scheduled_prompts(request: Request) -> list[ScheduledPromptResponse]:
    """List every scheduled prompt."""
    scheduled_prompts = await request.app.state.scheduled_prompt_repository.list_all()
    return [ScheduledPromptResponse.of(scheduled) for scheduled in scheduled_prompts]


@router.get("/{scheduled_id}", response_model=ScheduledPromptResponse)
async def get_scheduled_prompt(request: Request, scheduled_id: str) -> ScheduledPromptResponse:
    """Fetch one scheduled prompt by id."""
    scheduled = await request.app.state.scheduled_prompt_repository.get(scheduled_id)
    if scheduled is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such scheduled prompt.")
    return ScheduledPromptResponse.of(scheduled)


@router.patch("/{scheduled_id}", response_model=ScheduledPromptResponse)
async def update_scheduled_prompt(
    request: Request, scheduled_id: str, body: ScheduledPromptUpdate
) -> ScheduledPromptResponse:
    """Partially update a scheduled prompt."""
    scheduled = await request.app.state.scheduled_prompt_repository.get(scheduled_id)
    if scheduled is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such scheduled prompt.")

    updates = body.model_dump(exclude_unset=True)
    if "repository" in updates:
        _ensure_repository_allowed(request, updates["repository"])
    for field, value in updates.items():
        setattr(scheduled, field, value)
    scheduled.touch()

    saved = await request.app.state.scheduled_prompt_repository.save(scheduled)
    if saved.organization_id is not None:
        await _sync_dashboard(request, saved.organization_id)
    return ScheduledPromptResponse.of(saved)


@router.delete("/{scheduled_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_scheduled_prompt(request: Request, scheduled_id: str) -> None:
    """Remove a scheduled prompt."""
    scheduled = await request.app.state.scheduled_prompt_repository.get(scheduled_id)
    await request.app.state.scheduled_prompt_repository.delete(scheduled_id)
    if scheduled is not None and scheduled.organization_id is not None:
        await _sync_dashboard(request, scheduled.organization_id)
