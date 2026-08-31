"""Area detection from changed file paths."""

import pytest

from app.core.area_detector import AREA_RULES, detect_areas, is_test_file
from app.models.common import Area
from app.models.github import ChangedFile, FileChangeStatus


def _files(*paths: str) -> list[ChangedFile]:
    return [ChangedFile(filename=p, status=FileChangeStatus.MODIFIED) for p in paths]


def _areas(*paths: str) -> set[Area]:
    return set(detect_areas(_files(*paths)).areas)


# --- Security-sensitive areas ------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "app/auth/middleware.py",
        "src/login/handler.go",
        "lib/session_store.rb",
        "api/oauth_callback.py",
        "internal/jwt/verify.go",
        "services/password_reset.py",
    ],
)
def test_authentication_paths(path: str) -> None:
    assert Area.AUTHENTICATION in _areas(path)


@pytest.mark.parametrize(
    "path",
    ["app/authz/policy.py", "src/permissions.ts", "lib/rbac/roles.go", "api/access_control.py"],
)
def test_authorization_paths(path: str) -> None:
    assert Area.AUTHORIZATION in _areas(path)


def test_authorization_does_not_falsely_trigger_authentication() -> None:
    """`authoriz` contains `auth`; the negative lookahead must prevent a double hit."""
    areas = _areas("app/authorization/policy.py")

    assert Area.AUTHORIZATION in areas
    assert Area.AUTHENTICATION not in areas


@pytest.mark.parametrize(
    "path", ["app/security/crypto.py", "lib/encryption.go", "src/sanitize_input.ts", "app/csrf.py"]
)
def test_security_paths(path: str) -> None:
    assert Area.SECURITY in _areas(path)


@pytest.mark.parametrize(
    "path", ["billing/invoice.py", "src/stripe_client.ts", "app/checkout/charge.go", "payments/refund.py"]
)
def test_payment_paths(path: str) -> None:
    assert Area.PAYMENTS in _areas(path)


# --- Data and infrastructure -------------------------------------------------


@pytest.mark.parametrize(
    "path",
    ["migrations/0004_add_oauth.sql", "alembic/versions/abc.py", "db/migrate/20240101_x.rb", "schema.sql"],
)
def test_migration_paths(path: str) -> None:
    assert Area.MIGRATIONS in _areas(path)


@pytest.mark.parametrize(
    "path", ["infra/main.tf", "Dockerfile", "docker-compose.yml", "k8s/deployment.yaml", "helm/values.yaml"]
)
def test_infrastructure_paths(path: str) -> None:
    assert Area.INFRASTRUCTURE in _areas(path)


@pytest.mark.parametrize(
    "path", [".github/workflows/deploy.yml", ".gitlab-ci.yml", "Jenkinsfile", ".circleci/config.yml"]
)
def test_ci_paths(path: str) -> None:
    assert Area.CI_CD in _areas(path)


@pytest.mark.parametrize(
    "path", ["api/users.py", "src/routes/index.ts", "app/controllers/orders.rb", "schema.graphql"]
)
def test_api_paths(path: str) -> None:
    assert Area.API in _areas(path)


@pytest.mark.parametrize(
    "path",
    ["requirements.txt", "package.json", "poetry.lock", "go.mod", "Cargo.toml", "yarn.lock"],
)
def test_dependency_paths(path: str) -> None:
    assert Area.DEPENDENCIES in _areas(path)


@pytest.mark.parametrize(
    "path", ["src/styles/main.css", "components/Button.tsx", "pages/index.vue", "public/index.html"]
)
def test_frontend_paths(path: str) -> None:
    assert Area.FRONTEND in _areas(path)


@pytest.mark.parametrize("path", ["README.md", "docs/setup.rst", "CHANGELOG.md"])
def test_documentation_paths(path: str) -> None:
    assert Area.DOCUMENTATION in _areas(path)


@pytest.mark.parametrize(
    "path", ["app/prompts/review.txt", "src/llm_client.py", "lib/openai_wrapper.ts", "agents/reviewer.py"]
)
def test_ai_paths(path: str) -> None:
    assert Area.AI in _areas(path)


# --- Tests -------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "tests/test_auth.py",
        "src/__tests__/login.test.ts",
        "spec/models/user_spec.rb",
        "app/auth/middleware.test.js",
        "tests/conftest.py",
    ],
)
def test_test_files_are_recognised(path: str) -> None:
    assert is_test_file(path) is True


@pytest.mark.parametrize(
    "path",
    [
        "app/auth/middleware.py",
        "src/latest/index.ts",
        "contest/rules.md",
        # Regression: "test_" must be anchored to a path segment, not matched
        # as a bare substring — these all contain "test_" mid-word and are
        # not test files at all.
        "app/services/latest_transactions.py",
        "app/contest_winners.py",
        "app/protest_data.py",
    ],
)
def test_production_files_are_not_mistaken_for_tests(path: str) -> None:
    assert is_test_file(path) is False


def test_test_files_score_as_tests_only() -> None:
    """Editing a test cannot break production auth, so it must not score as auth."""
    areas = _areas("tests/test_auth_middleware.py")

    assert areas == {Area.TESTING}
    assert Area.AUTHENTICATION not in areas


def test_test_only_pr_is_flagged() -> None:
    detection = detect_areas(_files("tests/test_a.py", "tests/test_b.py"))

    assert detection.test_only is True
    assert detection.areas == {Area.TESTING}


def test_mixed_pr_is_not_test_only() -> None:
    detection = detect_areas(_files("tests/test_auth.py", "app/auth/middleware.py"))

    assert detection.test_only is False
    assert Area.AUTHENTICATION in detection.areas
    assert Area.TESTING in detection.areas


# --- Multi-area and evidence -------------------------------------------------


def test_a_file_can_belong_to_several_areas() -> None:
    """`api/auth/login.py` is both an API surface and authentication."""
    areas = _areas("api/auth/login.py")

    assert {Area.AUTHENTICATION, Area.API}.issubset(areas)


def test_realistic_oauth_pull_request() -> None:
    """The scenario from the flow diagram."""
    areas = _areas(
        "app/auth/middleware.py",
        "app/api/users.py",
        "migrations/0004_oauth.sql",
        "tests/test_auth.py",
        "README.md",
    )

    assert {Area.AUTHENTICATION, Area.API, Area.MIGRATIONS, Area.TESTING, Area.DOCUMENTATION}.issubset(areas)


def test_evidence_records_the_triggering_files() -> None:
    detection = detect_areas(_files("app/auth/middleware.py", "app/auth/session.py"))

    auth = next(e for e in detection.evidence if e.area is Area.AUTHENTICATION)
    assert set(auth.files) == {"app/auth/middleware.py", "app/auth/session.py"}
    assert auth.reason


def test_paths_are_matched_case_insensitively() -> None:
    assert Area.AUTHENTICATION in _areas("App/Auth/Middleware.py")


def test_windows_separators_are_handled() -> None:
    assert Area.AUTHENTICATION in _areas(r"app\auth\middleware.py")


# --- Edges -------------------------------------------------------------------


def test_no_files_detects_nothing() -> None:
    detection = detect_areas([])

    assert not detection
    assert detection.areas == frozenset()
    assert detection.test_only is False


def test_blank_filenames_are_skipped() -> None:
    assert detect_areas(_files("")).areas == frozenset()


def test_unrecognised_file_yields_no_area() -> None:
    assert _areas("weird.xyz") == set()


def test_every_rule_targets_a_real_area() -> None:
    assert all(isinstance(rule.area, Area) for rule in AREA_RULES)


def test_detection_is_reproducible() -> None:
    paths = ("app/auth/x.py", "api/y.py", "migrations/z.sql")

    assert _areas(*paths) == _areas(*paths)
