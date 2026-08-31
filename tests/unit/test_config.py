"""Configuration loading, defaults, and validation."""

import pytest
from pydantic import ValidationError

from app.core.config import ModelProvider, SandboxMode, Settings, get_settings


def test_loads_required_values_from_environment() -> None:
    settings = get_settings()

    assert settings.linear_webhook_secret == "test-linear-webhook-secret"
    assert settings.aida_mate_linear_actor_id == "aida-mate-actor-id"
    assert settings.openai_api_key == "test-openai-key"


def test_defaults() -> None:
    settings = get_settings()

    assert settings.app_env == "local"
    assert settings.log_level == "INFO"
    assert settings.model_provider is ModelProvider.OPENAI
    assert settings.review_model == "gpt-5.6-sol"
    assert settings.utility_model == "gpt-5.6-luna"
    assert settings.risk_low_max_score == 20
    assert settings.risk_medium_max_score == 60
    assert settings.medium_requires_human_review is False
    assert settings.sandbox_timeout_seconds == 900
    assert settings.sandbox_binary == "docker"
    assert settings.agent_timeout_seconds == 300
    assert settings.specialist_timeout_seconds == 60
    assert settings.auto_merge_on_done_enabled is False
    assert settings.public_base_url == "http://localhost:8000"
    assert settings.github_webhook_secret is None
    assert settings.github_merge_sync_enabled is False
    assert settings.github_merge_sync_branch == "main"
    assert settings.github_issue_sync_enabled is False
    assert settings.linear_sync_team_key is None
    assert settings.scheduled_prompts_enabled is False


def test_missing_webhook_secret_is_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LINEAR_WEBHOOK_SECRET", raising=False)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_openai_provider_requires_openai_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODEL_PROVIDER", "openai")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with pytest.raises(ValidationError, match="OPENAI_API_KEY"):
        Settings(_env_file=None)


def test_anthropic_provider_requires_anthropic_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODEL_PROVIDER", "anthropic")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    with pytest.raises(ValidationError, match="ANTHROPIC_API_KEY"):
        Settings(_env_file=None)


def test_unused_provider_key_is_not_required(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the selected provider's key is mandatory."""
    monkeypatch.setenv("MODEL_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    settings = Settings(_env_file=None)

    assert settings.model_provider is ModelProvider.ANTHROPIC
    assert settings.openai_api_key is None


@pytest.mark.parametrize(
    ("env_value", "expected"),
    [("debug", "DEBUG"), ("Warning", "WARNING"), ("ERROR", "ERROR")],
)
def test_log_level_is_case_insensitive(
    monkeypatch: pytest.MonkeyPatch, env_value: str, expected: str
) -> None:
    monkeypatch.setenv("LOG_LEVEL", env_value)

    assert Settings(_env_file=None).log_level == expected


def test_invalid_log_level_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_LEVEL", "VERBOSE")

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_model_provider_is_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODEL_PROVIDER", "OpenAI")

    assert Settings(_env_file=None).model_provider is ModelProvider.OPENAI


def test_misordered_risk_thresholds_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """A MEDIUM ceiling at or below the LOW ceiling makes the buckets ill-defined."""
    monkeypatch.setenv("RISK_LOW_MAX_SCORE", "60")
    monkeypatch.setenv("RISK_MEDIUM_MAX_SCORE", "20")

    with pytest.raises(ValidationError, match="RISK_MEDIUM_MAX_SCORE"):
        Settings(_env_file=None)


def test_risk_thresholds_are_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RISK_LOW_MAX_SCORE", "10")
    monkeypatch.setenv("RISK_MEDIUM_MAX_SCORE", "40")

    settings = Settings(_env_file=None)

    assert (settings.risk_low_max_score, settings.risk_medium_max_score) == (10, 40)


def test_capability_flags_reflect_missing_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no sandbox binary on PATH and no GitHub/Linear credentials, capabilities report False."""
    monkeypatch.setattr("shutil.which", lambda _: None)
    settings = get_settings()

    assert settings.sandbox_configured is False
    assert settings.github_app_configured is False
    assert settings.linear_oauth_configured is False


def test_sandbox_configured_when_binary_is_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pure PATH check — `docker sandbox` needs no secret."""
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/docker")

    assert Settings(_env_file=None).sandbox_configured is True


def test_sandbox_configured_is_false_when_binary_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _: None)

    assert Settings(_env_file=None).sandbox_configured is False


def test_sandbox_binary_is_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SANDBOX_BINARY", "custom-docker")
    seen: list[str] = []
    monkeypatch.setattr("shutil.which", lambda name: seen.append(name) or None)

    _ = Settings(_env_file=None).sandbox_configured

    assert seen == ["custom-docker"]


def test_sandbox_mode_defaults_to_docker() -> None:
    assert Settings(_env_file=None).sandbox_mode is SandboxMode.DOCKER


def test_sandbox_mode_local_is_configured_without_checking_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """`local` needs no external binary, so it must never touch `shutil.which`."""
    monkeypatch.setenv("SANDBOX_MODE", "local")
    seen: list[str] = []
    monkeypatch.setattr("shutil.which", lambda name: seen.append(name) or None)

    settings = Settings(_env_file=None)

    assert settings.sandbox_configured is True
    assert seen == []


def test_sandbox_mode_accepts_mixed_case(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SANDBOX_MODE", "Local")

    assert Settings(_env_file=None).sandbox_mode is SandboxMode.LOCAL


# --- specialist_timeout_seconds ----------------------------------------------
#
# Bounds the per-agent budget inside one multi-agent analyze() run — without
# it, one hung specialist would stall the whole concurrent batch until the
# outer agent_timeout_seconds fired and failed the entire review.


def test_specialist_timeout_seconds_is_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPECIALIST_TIMEOUT_SECONDS", "120")

    assert Settings(_env_file=None).specialist_timeout_seconds == 120


def test_specialist_timeout_seconds_rejects_a_value_below_the_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SPECIALIST_TIMEOUT_SECONDS", "5")

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_specialist_timeout_seconds_rejects_a_value_above_the_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SPECIALIST_TIMEOUT_SECONDS", "700")

    with pytest.raises(ValidationError):
        Settings(_env_file=None)
