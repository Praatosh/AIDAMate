"""Deterministic area detection from changed file paths.

The flow diagram splits classification into two boxes: *area detection* ("based
on file paths, keywords and patterns") and *risk determination*. This module is
the first box.

Everything here is pure pattern matching over paths — no LLM, no network. That
matters for three reasons:

1. **It works today**, before the agent and sandbox exist.
2. **It is reproducible.** The same PR always yields the same areas, which is
   what lets the risk score be auditable.
3. **It cannot be prompt-injected.** A malicious PR cannot talk a regular
   expression into ignoring `auth/middleware.py`.

The LLM layer, when it arrives, *adds* findings on top; it never replaces this.
"""

import re
from dataclasses import dataclass

from app.models.common import Area
from app.models.github import ChangedFile


@dataclass(frozen=True)
class AreaRule:
    """One area and the path patterns that imply it."""

    area: Area
    pattern: re.Pattern[str]
    reason: str


def _rule(area: Area, fragments: list[str], reason: str) -> AreaRule:
    """Compile a rule from path fragments, matched anywhere in the path."""
    return AreaRule(area=area, pattern=re.compile("|".join(fragments)), reason=reason)


#: Paths that mark a file as a test. Checked first — see `detect_areas`.
_TEST_PATTERN = re.compile(
    r"(^|/)tests?/|(^|/)__tests__/|(^|/)spec/|_test\.|\.test\.|(^|/)test_|\.spec\.|conftest\.py|_spec\."
)

#: Order is irrelevant to the result — every rule is evaluated against every
#: file and the matches are unioned. It is arranged by risk weight purely for
#: readability.
AREA_RULES: tuple[AreaRule, ...] = (
    _rule(
        Area.AUTHENTICATION,
        [r"auth(?!or)", r"login", r"logout", r"signin", r"signup", r"session", r"jwt", r"oauth",
         r"password", r"credential", r"mfa", r"2fa", r"saml", r"openid"],
        "Touches authentication code",
    ),
    _rule(
        Area.AUTHORIZATION,
        [r"authz", r"authoriz", r"permission", r"\brole", r"policy", r"\bacl\b", r"rbac",
         r"access[_-]?control", r"guard", r"entitlement"],
        "Touches authorization or access control",
    ),
    _rule(
        Area.SECURITY,
        [r"security", r"crypto", r"encrypt", r"decrypt", r"secret", r"vault", r"sanitiz",
         r"csrf", r"xss", r"cors", r"\bhash", r"signature", r"certificate", r"\btls\b", r"\bssl\b"],
        "Touches security-sensitive code",
    ),
    _rule(
        Area.PAYMENTS,
        [r"payment", r"billing", r"invoice", r"stripe", r"paypal", r"checkout", r"subscription",
         r"\bcharge", r"refund", r"pricing", r"wallet", r"payout"],
        "Touches payment or billing logic",
    ),
    _rule(
        Area.MIGRATIONS,
        [r"(^|/)migrations?/", r"(^|/)alembic/", r"\.sql$", r"schema\.rb$", r"(^|/)flyway/",
         r"liquibase", r"(^|/)db/migrate/"],
        "Contains a database migration",
    ),
    _rule(
        Area.DATABASE,
        [r"(^|/)models?/", r"(^|/)entities/", r"(^|/)repositor(y|ies)/", r"(^|/)dao/", r"\borm\b",
         r"schema", r"database", r"(^|/)db/", r"prisma", r"sequelize", r"sqlalchemy"],
        "Touches database models or queries",
    ),
    _rule(
        Area.INFRASTRUCTURE,
        [r"\.tf$", r"\.tfvars$", r"terraform", r"dockerfile", r"docker-compose", r"(^|/)k8s/",
         r"kubernetes", r"(^|/)helm/", r"ansible", r"pulumi", r"cloudformation", r"\.tfstate"],
        "Changes infrastructure definitions",
    ),
    _rule(
        Area.CI_CD,
        [r"\.github/workflows/", r"\.gitlab-ci", r"jenkinsfile", r"\.circleci/", r"azure-pipelines",
         r"\.travis", r"bitbucket-pipelines", r"(^|/)\.buildkite/"],
        "Changes CI/CD pipelines",
    ),
    _rule(
        Area.API,
        [r"(^|/)api/", r"(^|/)routes?/", r"(^|/)endpoints?/", r"(^|/)controllers?/",
         r"(^|/)handlers?/", r"openapi", r"swagger", r"graphql", r"\.proto$", r"(^|/)resolvers?/",
         r"(^|/)views?\.py$", r"urls\.py$"],
        "Changes API surface",
    ),
    _rule(
        Area.AI,
        # `\bllm\b` would miss `llm_client.py`: an underscore is a word
        # character, so there is no word boundary after "llm".
        [r"prompt", r"(^|[/_.\-])llm([/_.\-]|$)", r"openai", r"anthropic", r"(^|/)agents?/",
         r"embedding", r"langchain", r"vector[_-]?store", r"completion", r"inference"],
        "Touches AI/LLM integration",
    ),
    _rule(
        Area.DEPENDENCIES,
        [r"requirements[^/]*\.txt$", r"package\.json$", r"package-lock\.json$", r"yarn\.lock$",
         r"pnpm-lock", r"poetry\.lock$", r"pyproject\.toml$", r"gemfile", r"go\.(mod|sum)$",
         r"cargo\.(toml|lock)$", r"composer\.(json|lock)$", r"pipfile"],
        "Changes dependencies",
    ),
    _rule(
        Area.FRONTEND,
        [r"\.(css|scss|sass|less)$", r"\.html$", r"\.(jsx|tsx|vue|svelte)$", r"(^|/)components?/",
         r"(^|/)pages?/", r"(^|/)styles?/", r"(^|/)public/", r"(^|/)assets/", r"tailwind",
         r"(^|/)ui/"],
        "Changes frontend or UI code",
    ),
    _rule(
        Area.CONFIGURATION,
        [r"\.env", r"config", r"settings", r"\.ini$", r"\.cfg$", r"\.properties$",
         r"(^|/)conf/"],
        "Changes configuration",
    ),
    _rule(
        Area.DOCUMENTATION,
        [r"\.md$", r"\.rst$", r"\.adoc$", r"(^|/)docs?/", r"readme", r"changelog", r"license",
         r"contributing"],
        "Documentation only",
    ),
    _rule(
        Area.BACKEND,
        [r"\.(py|go|java|rb|rs|cs|kt|scala|php|ex|exs)$", r"(^|/)services?/", r"(^|/)server/",
         r"(^|/)backend/", r"(^|/)lib/", r"(^|/)core/", r"(^|/)domain/"],
        "Changes backend code",
    ),
)


@dataclass(frozen=True)
class AreaEvidence:
    """Why an area was attributed, for the audit trail."""

    area: Area
    reason: str
    files: tuple[str, ...]


@dataclass(frozen=True)
class AreaDetection:
    """Result of scanning a pull request's changed files."""

    areas: frozenset[Area]
    evidence: tuple[AreaEvidence, ...]
    test_only: bool

    def __bool__(self) -> bool:
        """Whether anything was detected at all."""
        return bool(self.areas)


def _normalize(path: str) -> str:
    """Lowercase and forward-slash a path so patterns match across platforms."""
    return path.replace("\\", "/").lower()


def is_test_file(path: str) -> bool:
    """Whether a path looks like a test rather than production code."""
    return bool(_TEST_PATTERN.search(_normalize(path)))


def detect_areas(changed_files: list[ChangedFile]) -> AreaDetection:
    """Attribute areas to a pull request from its changed file paths.

    Test files are attributed to `TESTING` **only**, even when their path also
    matches a higher-weighted rule. Editing `tests/test_auth.py` cannot break
    production authentication, so scoring it as an auth change would inflate
    the risk of every PR that adds coverage — and discourage adding it.

    The spec's caveat, that a test change can still signal a security-sensitive
    area, is left to the LLM findings layer, which can say so explicitly rather
    than being inferred from a filename.
    """
    matched: dict[Area, tuple[str, list[str]]] = {}
    has_production_file = False

    for changed in changed_files:
        path = _normalize(changed.filename)
        if not path:
            continue

        if is_test_file(path):
            entry = matched.setdefault(Area.TESTING, ("Changes tests", []))
            entry[1].append(changed.filename)
            continue

        has_production_file = True
        for rule in AREA_RULES:
            if rule.pattern.search(path):
                entry = matched.setdefault(rule.area, (rule.reason, []))
                entry[1].append(changed.filename)

    evidence = tuple(
        AreaEvidence(area=area, reason=reason, files=tuple(files))
        for area, (reason, files) in sorted(matched.items(), key=lambda item: item[0].value)
    )

    return AreaDetection(
        areas=frozenset(matched),
        evidence=evidence,
        test_only=bool(matched) and not has_production_file,
    )
