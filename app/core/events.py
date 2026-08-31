"""Review lifecycle event names.

Centralised so log queries and dashboards can rely on stable strings rather
than ad-hoc message text scattered across modules. Every event is logged with
at least `review_id`; most also carry `linear_issue_id` and `github_pr`.
"""

from typing import Final

REVIEW_CREATED: Final = "REVIEW_CREATED"
SANDBOX_CREATED: Final = "SANDBOX_CREATED"
#: Archive uploaded and extracted — the sandbox's tree is ready for tool
#: calls. There is no separate "repository cloned" / "PR checked out" event:
#: the archive-download design has no git clone or checkout step (the host
#: has no `git`, by design), so a name implying one would be dishonest.
SANDBOX_READY: Final = "SANDBOX_READY"
SANDBOX_DESTROYED: Final = "SANDBOX_DESTROYED"
PR_RESOLVED: Final = "PR_RESOLVED"
PR_FETCHED: Final = "PR_FETCHED"
ANALYSIS_STARTED: Final = "ANALYSIS_STARTED"
#: The AI stage was skipped in favor of reusing a previous COMPLETED review's
#: findings for the exact same `content_key` (issue+PR+head_sha) — an explicit
#: re-delegation of an unchanged revision, not a genuinely new one.
ANALYSIS_REUSED: Final = "ANALYSIS_REUSED"
AGENT_STARTED: Final = "AGENT_STARTED"
#: One per tool invocation the agent actually makes (list_files/read_file/
#: search_code). Concrete, checkable evidence that the model is reasoning
#: about real repository content rather than the deterministic path scan.
AGENT_TOOL_CALL: Final = "AGENT_TOOL_CALL"
AGENT_COMPLETED: Final = "AGENT_COMPLETED"

#: Per-specialist spans inside the outer AGENT_STARTED/AGENT_COMPLETED pair —
#: the multi-agent pipeline `review_agent.py` runs internally. No
#: JUDGE_AGENT_FAILED: a Judge failure is a whole-review failure, already
#: covered by REVIEW_FAILED, since there is no reasonable partial result
#: without a synthesis step.
CONTEXT_AGENT_STARTED: Final = "CONTEXT_AGENT_STARTED"
CONTEXT_AGENT_COMPLETED: Final = "CONTEXT_AGENT_COMPLETED"
CONTEXT_AGENT_FAILED: Final = "CONTEXT_AGENT_FAILED"
SECURITY_AGENT_STARTED: Final = "SECURITY_AGENT_STARTED"
SECURITY_AGENT_COMPLETED: Final = "SECURITY_AGENT_COMPLETED"
SECURITY_AGENT_FAILED: Final = "SECURITY_AGENT_FAILED"
CODE_AGENT_STARTED: Final = "CODE_AGENT_STARTED"
CODE_AGENT_COMPLETED: Final = "CODE_AGENT_COMPLETED"
CODE_AGENT_FAILED: Final = "CODE_AGENT_FAILED"
ARCHITECTURE_AGENT_STARTED: Final = "ARCHITECTURE_AGENT_STARTED"
ARCHITECTURE_AGENT_COMPLETED: Final = "ARCHITECTURE_AGENT_COMPLETED"
ARCHITECTURE_AGENT_FAILED: Final = "ARCHITECTURE_AGENT_FAILED"
TESTING_AGENT_STARTED: Final = "TESTING_AGENT_STARTED"
TESTING_AGENT_COMPLETED: Final = "TESTING_AGENT_COMPLETED"
TESTING_AGENT_FAILED: Final = "TESTING_AGENT_FAILED"
JUDGE_AGENT_STARTED: Final = "JUDGE_AGENT_STARTED"
JUDGE_AGENT_COMPLETED: Final = "JUDGE_AGENT_COMPLETED"
ANALYSIS_COMPLETED: Final = "ANALYSIS_COMPLETED"
RISK_CLASSIFIED: Final = "RISK_CLASSIFIED"
GITHUB_UPDATED: Final = "GITHUB_UPDATED"
LINEAR_UPDATED: Final = "LINEAR_UPDATED"
REVIEW_COMPLETED: Final = "REVIEW_COMPLETED"
REVIEW_FAILED: Final = "REVIEW_FAILED"
REVIEW_SKIPPED_DUPLICATE: Final = "REVIEW_SKIPPED_DUPLICATE"

ALL_EVENTS: Final[frozenset[str]] = frozenset(
    {
        REVIEW_CREATED,
        SANDBOX_CREATED,
        SANDBOX_READY,
        SANDBOX_DESTROYED,
        PR_RESOLVED,
        PR_FETCHED,
        ANALYSIS_STARTED,
        ANALYSIS_REUSED,
        AGENT_STARTED,
        AGENT_TOOL_CALL,
        AGENT_COMPLETED,
        CONTEXT_AGENT_STARTED,
        CONTEXT_AGENT_COMPLETED,
        CONTEXT_AGENT_FAILED,
        SECURITY_AGENT_STARTED,
        SECURITY_AGENT_COMPLETED,
        SECURITY_AGENT_FAILED,
        CODE_AGENT_STARTED,
        CODE_AGENT_COMPLETED,
        CODE_AGENT_FAILED,
        ARCHITECTURE_AGENT_STARTED,
        ARCHITECTURE_AGENT_COMPLETED,
        ARCHITECTURE_AGENT_FAILED,
        TESTING_AGENT_STARTED,
        TESTING_AGENT_COMPLETED,
        TESTING_AGENT_FAILED,
        JUDGE_AGENT_STARTED,
        JUDGE_AGENT_COMPLETED,
        ANALYSIS_COMPLETED,
        RISK_CLASSIFIED,
        GITHUB_UPDATED,
        LINEAR_UPDATED,
        REVIEW_COMPLETED,
        REVIEW_FAILED,
        REVIEW_SKIPPED_DUPLICATE,
    }
)
