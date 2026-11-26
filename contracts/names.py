"""Every shared name and value in design.md, as Python constants.

Source of truth: openspec/changes/build-rollout-marshal/design.md. Each block
names the design.md section it comes from. Do not add, rename or change a
value here without first changing design.md through a contract-change pull
request. contracts/tests/test_contracts.py checks this file against design.md
in both directions, and contracts/names.json is generated from it for the
Terraform root (make names-json).
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC as _UTC
from datetime import datetime
from typing import NamedTuple

# ---------------------------------------------------------------------------
# Section 2. Fixed environment
# ---------------------------------------------------------------------------
ACCOUNT_ID = "818928662830"
REGION = "us-east-1"
RESOURCE_PREFIX = "rm-"
PYTHON_VERSION = "3.13"
LAMBDA_ARCHITECTURE = "arm64"
INFRA_DIR = "infra/"
TFSTATE_BUCKET = "rm-tfstate-818928662830"
TFSTATE_KEY = "rollout-marshal/terraform.tfstate"
TASK_TIMEBOX_MINUTES = 90
FLAGSMITH_ORGANISATION_ID = 41656
FLAGSMITH_PROJECT_NAME = "Toggle Wars"
FLAGSMITH_PROJECT_ID = 55601
FLAGSMITH_ENVIRONMENT_NAME = "Development"
FLAGSMITH_MONTHLY_REQUEST_LIMIT = 50_000
REPO = "github.com/abebars/rollout-marshal"

# ---------------------------------------------------------------------------
# Section 3. Bedrock models
# ---------------------------------------------------------------------------
MODEL_CURRENT = "us.anthropic.claude-fable-5-1"  # control arm; thinking always on
MODEL_CHEAPER = "us.anthropic.claude-sonnet-5"  # treatment arm
MODEL_JUDGE = "us.anthropic.claude-haiku-5-5"
MODEL_AGENT = "us.anthropic.claude-sonnet-5"  # plan, incident note, report prose
DATA_RETENTION_MODE = "aws_review"  # needed by MODEL_CURRENT in us-east-1
BEDROCK_RUNTIME_SERVICE = "bedrock-runtime"  # every call uses its Converse API
FORBIDDEN_SAMPLING_PARAMETERS = ("temperature", "top_p", "top_k")

ANSWER_MAX_TOKENS = 2000  # caps thinking plus text
ANSWER_THINKING_EFFORT = "low"
# Where the effort goes inside additionalModelRequestFields. Unproven until
# task 1.2 records the exact request fields; 1.4 corrects it if needed.
THINKING_EFFORT_FIELD = "output_config.effort"
ANSWER_MAX_WORDS = 120  # the answer system prompt asks for at most this many words
JUDGE_MAX_TOKENS = 200  # judge runs with thinking disabled
AGENT_MAX_TOKENS = 4000
STOP_REASON_MAX_TOKENS = "max_tokens"  # always an error, never a result

ANSWER_TIMEOUT_SECONDS = 25
ROUTER_TIMEOUT_SECONDS = 29
JUDGE_TIMEOUT_SECONDS = 30

# ---------------------------------------------------------------------------
# Section 4. Flagsmith contract
# ---------------------------------------------------------------------------
# Run ID: "r" + UTC YYYYMMDDHHMM (e.g. r202611271530); scratch runs from spikes
# and tests: "r" + a word + UTC HHMM (e.g. rsmoke0412).
RUN_ID_PREFIX = "r"
RUN_ID_PATTERN = r"^r[a-z]*[0-9]{4,12}$"
RUN_ID_TIME_FORMAT = "%Y%m%d%H%M"
SCRATCH_RUN_ID_TIME_FORMAT = "%H%M"

VARIANT_CURRENT = "current"
VARIANT_CHEAPER = "cheaper"
VARIANTS = (VARIANT_CURRENT, VARIANT_CHEAPER)
MODEL_BY_VARIANT = {VARIANT_CURRENT: MODEL_CURRENT, VARIANT_CHEAPER: MODEL_CHEAPER}

EXPERIMENT_FEATURE_PREFIX = "assistant_model_"
EXPERIMENT_FEATURE_KEY_TEMPLATE = "assistant_model_<run_id>"  # multivariate, one per run
CONTROL_VALUE = VARIANT_CURRENT  # the feature's own value; Flagsmith reserves the control
TREATMENT_OPTION_KEY = VARIANT_CHEAPER  # an option without a key records no exposures
TREATMENT_OPTION_VALUE = VARIANT_CHEAPER
TREATMENT_PERCENT = 50

KILL_SWITCH_FEATURE = "assistant_cheaper_enabled"  # boolean
KILL_SWITCH_DEFAULT_ENABLED = True
DEFAULT_MODEL_FEATURE = "assistant_default_model"  # string; what "promote" changes
DEFAULT_MODEL_DEFAULT = VARIANT_CURRENT
DEFAULT_MODEL_VALUES = VARIANTS
CHAOS_FEATURE = "chaos_delay_cheaper"  # section 9 fallback only
PERMANENT_FEATURES = (KILL_SWITCH_FEATURE, DEFAULT_MODEL_FEATURE)
FEATURE_KEYS = (
    EXPERIMENT_FEATURE_KEY_TEMPLATE,
    KILL_SWITCH_FEATURE,
    DEFAULT_MODEL_FEATURE,
    CHAOS_FEATURE,
)
# Full definitions, so a get-or-create can check what it found.
PERMANENT_FEATURE_DEFINITIONS = {
    KILL_SWITCH_FEATURE: {"type": "boolean", "default": KILL_SWITCH_DEFAULT_ENABLED},
    DEFAULT_MODEL_FEATURE: {"type": "string", "default": DEFAULT_MODEL_DEFAULT},
}

# Events: one of each per answer, sent by rm-judge with a numeric value.
EVENT_HELPFUL = "answer_helpful"  # value 1 or 0, integer
EVENT_LATENCY_MS = "answer_latency_ms"
EVENT_COST_MICRO_USD = "answer_cost_micro_usd"
EVENTS = (EVENT_HELPFUL, EVENT_LATENCY_MS, EVENT_COST_MICRO_USD)

# Metrics: environment level, get-or-create. Each is the mean of one event.
METRIC_HELPFUL_RATE = "helpful_rate"  # primary
METRIC_MEAN_LATENCY_MS = "mean_latency_ms"
METRIC_MEAN_COST_MICRO_USD = "mean_cost_micro_usd"
METRICS = (METRIC_HELPFUL_RATE, METRIC_MEAN_LATENCY_MS, METRIC_MEAN_COST_MICRO_USD)
PRIMARY_METRIC = METRIC_HELPFUL_RATE
GUARDRAIL_METRICS = (METRIC_MEAN_LATENCY_MS, METRIC_MEAN_COST_MICRO_USD)
METRIC_AGGREGATIONS_AVAILABLE = ("occurrence", "count", "sum", "mean")
METRIC_AGGREGATION = "mean"
METRIC_DEFINITIONS = {
    METRIC_HELPFUL_RATE: {"event": EVENT_HELPFUL, "aggregation": METRIC_AGGREGATION},
    METRIC_MEAN_LATENCY_MS: {"event": EVENT_LATENCY_MS, "aggregation": METRIC_AGGREGATION},
    METRIC_MEAN_COST_MICRO_USD: {
        "event": EVENT_COST_MICRO_USD,
        "aggregation": METRIC_AGGREGATION,
    },
}

IDENTITY_COUNT = 150
IDENTITY_FIRST = "user-001"
IDENTITY_LAST = "user-150"
IDENTITY_PATTERN = r"^user-(00[1-9]|0[1-9][0-9]|1[0-4][0-9]|150)$"

FLAGSMITH_SDK_PACKAGE = "flagsmith"
FLAGSMITH_SDK_MIN_VERSION = "6.6.0"
FLAGSMITH_CLIENT_OPTIONS = {"enable_events": True}  # remote evaluation
# SDK calls; the exact signatures are proven by task 1.2.
SDK_FLAGS_CALL = "get_identity_flags"  # once per answer, the only identity request
SDK_EXPOSURE_CALL = "track_exposure_event"  # only when flag.experiment.in_experiment
SDK_OUTCOME_CALL = "track_event"  # with the identity and a numeric value
SDK_FLUSH_CALL = "flush_events"  # before every handler returns
SDK_FORBIDDEN_CALL = "get_experiment_flag"  # makes its own identity request
EXPOSURE_METADATA_KEY = "experiment_id"

FLAGSMITH_MCP_URL = "https://mcp.flagsmith.com"
FLAGSMITH_MCP_AUTH_HEADER_TEMPLATE = "Authorization: Api-Key <key>"
BOOTSTRAP_FLAGSMITH_SCRIPT = "scripts/bootstrap_flagsmith.py"

DEFAULT_MODEL_CACHE_SECONDS = 30  # router cache of assistant_default_model
RESULTS_REFRESH_MIN_SECONDS = 300  # results refreshed at most once per 5 minutes

# ---------------------------------------------------------------------------
# Section 5. Request path and metrics
# ---------------------------------------------------------------------------
ROUTER_FUNCTION = "rm-router"
ANSWER_CURRENT_FUNCTION = "rm-answer-current"
ANSWER_CHEAPER_FUNCTION = "rm-answer-cheaper"
JUDGE_FUNCTION = "rm-judge"
ANSWER_FUNCTION_BY_VARIANT = {
    VARIANT_CURRENT: ANSWER_CURRENT_FUNCTION,
    VARIANT_CHEAPER: ANSWER_CHEAPER_FUNCTION,
}
API_GATEWAY_TIMEOUT_SECONDS = 30  # applies to the request path only

# What rm-router hands rm-judge (contracts/schemas/judge_payload.json).
JUDGE_PAYLOAD_FIELDS = (
    "run_id",
    "identity",
    "assigned",
    "served",
    "model_id",
    "question",
    "answer",
    "input_tokens",
    "output_tokens",
    "latency_ms",
)

CW_NAMESPACE = "RolloutMarshal"
CW_DIMENSION_VARIANT = "Variant"  # the variant actually served
CW_DIMENSION_SETS = [[CW_DIMENSION_VARIANT]]  # every metric carries exactly this set
CW_METRIC_ANSWER_LATENCY_MS = "AnswerLatencyMs"  # emitted by rm-router
CW_METRIC_ANSWER_COST_MICRO_USD = "AnswerCostMicroUsd"  # emitted by rm-judge
CW_METRIC_ANSWER_HELPFUL = "AnswerHelpful"  # emitted by rm-judge, 0 or 1
CW_METRIC_UNITS = {
    CW_METRIC_ANSWER_LATENCY_MS: "Milliseconds",
    CW_METRIC_ANSWER_COST_MICRO_USD: "Count",
    CW_METRIC_ANSWER_HELPFUL: "Count",
}
CW_METRIC_EMITTER = {
    CW_METRIC_ANSWER_LATENCY_MS: ROUTER_FUNCTION,
    CW_METRIC_ANSWER_COST_MICRO_USD: JUDGE_FUNCTION,
    CW_METRIC_ANSWER_HELPFUL: JUDGE_FUNCTION,
}

PRICES_FILE = "app/prices.json"  # copied from the AWS Bedrock pricing page, with URL and date

ALARM_NAME = "rm-cheaper-latency-p95"
ALARM_METRIC = CW_METRIC_ANSWER_LATENCY_MS
ALARM_DIMENSION = f"{CW_DIMENSION_VARIANT}={VARIANT_CHEAPER}"
ALARM_STATISTIC = "p95"
ALARM_PERIOD_SECONDS = 60
ALARM_EVALUATION_PERIODS = 2
ALARM_DATAPOINTS_TO_ALARM = 2
ALARM_TREAT_MISSING_DATA = "notBreaching"
ALARM_THRESHOLD_MS = 10_000  # default; task 4.1 recalibrates it and records it in design.md
# OK and INSUFFICIENT_DATA come from the signals spec and tasks.md 2.4, not
# design.md; the test's OTHER_SOURCES lists them.
ALARM_STATE_OK = "OK"
ALARM_STATE_ALARM = "ALARM"
ALARM_STATE_INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
ALARM_STATES = (ALARM_STATE_OK, ALARM_STATE_ALARM, ALARM_STATE_INSUFFICIENT_DATA)

# ---------------------------------------------------------------------------
# Section 6. State
# ---------------------------------------------------------------------------
RUNS_TABLE = "rm-runs"
PARTITION_KEY = "pk"
ACTIVE_POINTER_PK = "ACTIVE"
RUN_PK_PREFIX = "RUN#"
RUN_PK_TEMPLATE = "RUN#<run_id>"
POINTER_ATTRIBUTES = ("run_id", "feature_key")  # feature_key is added when launch commits
POINTER_CREATE_CONDITION = "attribute_not_exists(pk)"
POINTER_CACHE_SECONDS = 30
SCHEMA_PLAN_FILE = "contracts/schemas/plan.json"

# Run item attributes and their DynamoDB types.
RUN_ITEM_ATTRIBUTES = {
    "run_id": "S",
    "state": "S",
    "prompt": "S",
    "plan": "M",
    "feature_key": "S",
    "experiment_id": "N",
    "metric_ids": "M",
    "incident": "M",
    "report_key": "S",
    "history": "L",
    "pending": "S",
    "pending_at": "S",
    "last_refresh_at": "S",
    "first_running_at": "S",
    "created_at": "S",
    "updated_at": "S",
}
INCIDENT_FIELDS = ("started_at", "cleared_at", "alarm_name")
HISTORY_FIELDS = ("at", "from", "to", "actor", "reason")
CLEAR_ACTIVE = "clear_active"  # conditional delete of the pointer on run_id

STATE_PLANNED = "PLANNED"
STATE_RUNNING = "RUNNING"
STATE_ROLLED_BACK = "ROLLED_BACK"
STATE_AWAITING_RESUME = "AWAITING_RESUME"
STATE_RESULTS_READY = "RESULTS_READY"
STATE_PROMOTED = "PROMOTED"
STATE_HELD = "HELD"
STATE_ABORTED = "ABORTED"
STATES = (
    STATE_PLANNED,
    STATE_RUNNING,
    STATE_ROLLED_BACK,
    STATE_AWAITING_RESUME,
    STATE_RESULTS_READY,
    STATE_PROMOTED,
    STATE_HELD,
    STATE_ABORTED,
)
TERMINAL_STATES = (STATE_PROMOTED, STATE_HELD, STATE_ABORTED)

ACTION_START = "start"
ACTION_APPROVE = "approve"
ACTION_DECLINE = "decline"
ACTION_STATUS = "status"
ACTION_CHECK = "check"
ACTION_ALARM = "alarm"
ACTION_TICK = "tick"
ACTIONS = (
    ACTION_START,
    ACTION_APPROVE,
    ACTION_DECLINE,
    ACTION_STATUS,
    ACTION_CHECK,
    ACTION_ALARM,
    ACTION_TICK,
)
# Only these return an error when not legal in the current state; alarm and
# tick are successful no-ops instead.
ACTIONS_WITH_LEGALITY_ERRORS = (ACTION_START, ACTION_APPROVE, ACTION_DECLINE, ACTION_CHECK)

ACTOR_ENGINEER = "engineer"
ACTOR_AGENT = "agent"
ACTORS = (ACTOR_ENGINEER, ACTOR_AGENT)


class Transition(NamedTuple):
    """One row of the transition table. source None means "(none)"."""

    source: str | None
    target: str
    triggers: tuple[str, ...]
    actor: str  # what history records for this transition


TRANSITIONS = (
    Transition(None, STATE_PLANNED, (ACTION_START,), ACTOR_ENGINEER),
    Transition(STATE_PLANNED, STATE_RUNNING, (ACTION_APPROVE,), ACTOR_ENGINEER),
    Transition(STATE_PLANNED, STATE_ABORTED, (ACTION_DECLINE,), ACTOR_ENGINEER),
    Transition(
        STATE_RUNNING,
        STATE_ROLLED_BACK,
        (ACTION_ALARM, ACTION_TICK, ACTION_CHECK),
        ACTOR_AGENT,
    ),
    Transition(STATE_RUNNING, STATE_RESULTS_READY, (ACTION_TICK, ACTION_CHECK), ACTOR_AGENT),
    Transition(STATE_RUNNING, STATE_HELD, (ACTION_DECLINE,), ACTOR_ENGINEER),
    Transition(
        STATE_ROLLED_BACK,
        STATE_AWAITING_RESUME,
        (ACTION_TICK, ACTION_CHECK),
        ACTOR_AGENT,
    ),
    Transition(STATE_ROLLED_BACK, STATE_HELD, (ACTION_DECLINE,), ACTOR_ENGINEER),
    Transition(STATE_AWAITING_RESUME, STATE_RUNNING, (ACTION_APPROVE,), ACTOR_ENGINEER),
    Transition(STATE_AWAITING_RESUME, STATE_ROLLED_BACK, (ACTION_APPROVE,), ACTOR_AGENT),
    Transition(STATE_AWAITING_RESUME, STATE_HELD, (ACTION_DECLINE,), ACTOR_ENGINEER),
    Transition(STATE_RESULTS_READY, STATE_PROMOTED, (ACTION_APPROVE,), ACTOR_ENGINEER),
    Transition(STATE_RESULTS_READY, STATE_HELD, (ACTION_DECLINE,), ACTOR_ENGINEER),
)
LEGAL_TRANSITIONS = frozenset((t.source, t.target) for t in TRANSITIONS)

CHECK_LEGAL_STATES = (STATE_RUNNING, STATE_ROLLED_BACK)
ALARM_NOOP_STATES = (STATE_AWAITING_RESUME, STATE_ROLLED_BACK, STATE_RESULTS_READY)
PENDING_STALE_SECONDS = 60  # a pending claim older than this may be resumed
RESULTS_EMBARGO_SECONDS = 600  # no results read in the first 600 s after first_running_at
PROBE_INVOKES = 5  # direct invokes of rm-answer-cheaper; passes if the slowest < threshold
MIN_IDENTITIES_PER_VARIATION = 50

# Guardrails: cheaper mean latency at most 110 percent of current's, and
# cheaper mean cost not higher than current's.
GUARDRAIL_MAX_LATENCY_RATIO = 1.10
GUARDRAIL_MAX_COST_RATIO = 1.00

RECOMMEND_PROMOTE = "promote"
RECOMMEND_HOLD = "hold"
RECOMMEND_HOLD_RERUN = "hold, re-run"  # after any incident
RECOMMENDATIONS = (RECOMMEND_PROMOTE, RECOMMEND_HOLD, RECOMMEND_HOLD_RERUN)
PROMOTE_MIN_PROB_NOT_WORSE = 0.95
# Fallback when prob_cheaper_not_worse is null: the lower bound of the credible
# interval for the lift on helpful_rate is at or above minus 5 percent. Encoded
# as a fraction of relative lift; the unit is a decision at the 1.1 gate.
PROMOTE_MIN_HELPFUL_LIFT_CI_LOWER = -0.05

# ---------------------------------------------------------------------------
# Section 7. Invoking the agent
# ---------------------------------------------------------------------------
AGENT_RUNTIME_NAME = "rm_agent"
SESSION_ID_PREFIX = "rm-session-"  # + UUID4
SESSION_ID_MIN_LENGTH = 33

AWAITS_LAUNCH = "launch"
AWAITS_RESUME = "resume"
AWAITS_PROMOTION = "promotion"
AWAITING_VALUES = (AWAITS_LAUNCH, AWAITS_RESUME, AWAITS_PROMOTION)
# The three engineer approvals: each approve-triggered row of the table from
# a state the run waits in. Every other state awaits nothing (null).
AWAITING_BY_STATE = {
    STATE_PLANNED: AWAITS_LAUNCH,
    STATE_AWAITING_RESUME: AWAITS_RESUME,
    STATE_RESULTS_READY: AWAITS_PROMOTION,
}

ERROR_ILLEGAL_IN_STATE = "ILLEGAL_IN_STATE"
ERROR_RUN_ACTIVE = "RUN_ACTIVE"
ERROR_NOT_FOUND = "NOT_FOUND"
ERROR_EXPERIMENT_NOT_ENDED = "EXPERIMENT_NOT_ENDED"
ERROR_EFFECT_FAILED = "EFFECT_FAILED"
ERROR_BAD_REQUEST = "BAD_REQUEST"
ERROR_CODES = (
    ERROR_ILLEGAL_IN_STATE,
    ERROR_RUN_ACTIVE,
    ERROR_NOT_FOUND,
    ERROR_EXPERIMENT_NOT_ENDED,
    ERROR_EFFECT_FAILED,
    ERROR_BAD_REQUEST,
)

INVOKE_FIELDS = ("action", "prompt", "actor", "run_id", "alarm_name", "alarm_state", "at")
RESPONSE_FIELDS = ("run_id", "state", "message", "awaiting", "error")
ERROR_FIELDS = ("code", "message")
RESULTS_FIELDS = ("ready", "variations", "prob_cheaper_not_worse", "helpful_lift_ci_lower")
VARIATION_FIELDS = ("identities", "helpful_rate", "mean_latency_ms", "mean_cost_micro_usd")

SCHEMA_DIR = "contracts/schemas/"
SCHEMA_NAMES = ("invoke", "response", "plan", "run_item", "results", "judge_payload")
SCHEMA_FILES = {name: f"{SCHEMA_DIR}{name}.json" for name in SCHEMA_NAMES}

CLI_NAME = "marshal"
CLI_ACTIONS = (ACTION_START, ACTION_APPROVE, ACTION_DECLINE, ACTION_STATUS, ACTION_CHECK)
INVOKER_FUNCTION = "rm-invoker"  # sends alarm and tick
AGENT_RUNTIME_ARN_PARAMETER = "/rm/agent-runtime-arn"
INVOKER_STUB_VARIABLE = "invoker_stub"  # Terraform root variable, set by the lead
INVOKER_STUB_ENV = "STUB"
INVOKER_STUB_ON = "1"  # with STUB=1 rm-invoker only logs the payload
TICK_SCHEDULE = "rm-tick"
TICK_INTERVAL_MINUTES = 2  # EventBridge Scheduler, enabled only while a run is active

FLAGSMITH_ADMIN_FUNCTION = "rm-flagsmith-admin"  # Gateway target
INCIDENTS_TOPIC = "rm-incidents"
REPORTS_BUCKET = "rm-reports-818928662830"
AGENT_TOOL_FLAGSMITH_ADMIN = "flagsmith_admin"
AGENT_TOOLS = (
    AGENT_TOOL_FLAGSMITH_ADMIN,
    "state",
    "signals",
    "probe",
    "schedule",
    "notify",
    "report",
)
GATEWAY_FALLBACK_AFTER_MINUTES = 90  # then connect straight to FLAGSMITH_MCP_URL

# ---------------------------------------------------------------------------
# Section 8. Secrets and notifications
# ---------------------------------------------------------------------------
SECRET_FLAGSMITH_API_KEY = "rm/flagsmith/api-key"  # organisation API key (MCP, Admin API)
SECRET_FLAGSMITH_SERVER_KEY = "rm/flagsmith/server-key"  # SDK key for rm-router, rm-judge
FLAGSMITH_API_KEY_NAME = "rollout-marshal-agent"

# ---------------------------------------------------------------------------
# Section 9. Fault injection
# ---------------------------------------------------------------------------
FIS_TEMPLATE = "rm-delay-cheaper"
FIS_ACTION = "aws:lambda:invocation-add-delay"
FIS_TARGET_FUNCTION = ANSWER_CHEAPER_FUNCTION
FIS_INVOCATION_PERCENTAGE = 100
FIS_DELAY_MS = 10_000
FIS_DURATION_MINUTES = 6
FIS_SLOW_POLL_ENV = "AWS_FIS_SLOW_POLL_INTERVAL_SECONDS"
FIS_SLOW_POLL_SECONDS = 10
FIS_TIMEBOX_MINUTES = 120

# ---------------------------------------------------------------------------
# Section 10. Report
# ---------------------------------------------------------------------------
REPORT_KEY_TEMPLATE = "runs/<run_id>/report.md"  # in REPORTS_BUCKET
REPORT_SECTIONS = (
    "Hypothesis",
    "Setup",
    "Timeline",
    "Results",
    "Guardrails",
    "Incident",
    "Known bias",
    "Recommendation",
    "Time taken",
)

# ---------------------------------------------------------------------------
# Section 11. Load
# ---------------------------------------------------------------------------
LOAD_SCRIPT = "scripts/load.py"
QUESTIONS_FILE = "app/questions.json"
QUESTIONS_COUNT = 30
LOAD_MAX_CONCURRENCY = 10


class LoadPlan(NamedTuple):
    users: int
    questions: int


LOAD_SMOKE = LoadPlan(150, 2)  # task 3.6
LOAD_FAULT = LoadPlan(150, 2)  # task 4.3
LOAD_FULL = LoadPlan(150, 10)  # 5.1 and each of the two runs in 5.2
ANSWER_BUDGET = 5_100  # 300 + 300 + 3 x 1,500
ANSWER_HARD_STOP = 7_500  # stop and ask the human before exceeding

# ---------------------------------------------------------------------------
# CLAUDE.md stop rules (operational limits from CLAUDE.md, not design.md; the
# test's OTHER_SOURCES lists them)
# ---------------------------------------------------------------------------
FLAGSMITH_USAGE_STOP = 35_000
ANSWER_COST_STOP_MICRO_USD = 60_000_000

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
_RUN_ID_RE = re.compile(RUN_ID_PATTERN)
_IDENTITY_RE = re.compile(IDENTITY_PATTERN)


def is_run_id(value: str) -> bool:
    """True when value matches RUN_ID_PATTERN."""
    return bool(_RUN_ID_RE.match(value))


def _require_run_id(run_id: str) -> str:
    if not is_run_id(run_id):
        raise ValueError(f"not a run ID (expected {RUN_ID_PATTERN}): {run_id!r}")
    return run_id


def _utc(now: datetime) -> datetime:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return now.astimezone(_UTC)


def run_id_for(now: datetime) -> str:
    """Run ID for a real run: "r" + UTC YYYYMMDDHHMM."""
    return RUN_ID_PREFIX + _utc(now).strftime(RUN_ID_TIME_FORMAT)


def scratch_run_id(word: str, now: datetime) -> str:
    """Run ID for a spike or test run: "r" + word + UTC HHMM, e.g. rsmoke0412."""
    if not re.fullmatch(r"[a-z]+", word):
        raise ValueError(f"scratch run word must be lowercase letters: {word!r}")
    return _require_run_id(RUN_ID_PREFIX + word + _utc(now).strftime(SCRATCH_RUN_ID_TIME_FORMAT))


def experiment_feature_key(run_id: str) -> str:
    """assistant_model_<run_id>."""
    return EXPERIMENT_FEATURE_PREFIX + _require_run_id(run_id)


def run_pk(run_id: str) -> str:
    """Partition key of a run item: RUN#<run_id>."""
    return RUN_PK_PREFIX + _require_run_id(run_id)


def report_key(run_id: str) -> str:
    """S3 key of a run's report in REPORTS_BUCKET: runs/<run_id>/report.md."""
    return REPORT_KEY_TEMPLATE.replace("<run_id>", _require_run_id(run_id))


def identity(n: int) -> str:
    """user-001 .. user-150."""
    if not 1 <= n <= IDENTITY_COUNT:
        raise ValueError(f"identity number must be 1..{IDENTITY_COUNT}: {n}")
    return f"user-{n:03d}"


def is_identity(value: str) -> bool:
    return bool(_IDENTITY_RE.match(value))


def new_session_id() -> str:
    """A fresh AgentCore runtime session ID: rm-session- + UUID4 (47 characters)."""
    return SESSION_ID_PREFIX + str(uuid.uuid4())


def awaiting_for(state: str | None) -> str | None:
    """What a run in this state waits for, or None."""
    return AWAITING_BY_STATE.get(state) if state else None


def as_json() -> str:
    """Every upper-case constant as JSON, for Terraform (jsondecode) and other tools."""

    def plain(value):
        if isinstance(value, frozenset):
            return sorted((plain(v) for v in value), key=json.dumps)
        if isinstance(value, tuple) and hasattr(value, "_asdict"):
            return {k: plain(v) for k, v in value._asdict().items()}
        if isinstance(value, list | tuple):
            return [plain(v) for v in value]
        if isinstance(value, dict):
            return {str(k): plain(v) for k, v in value.items()}
        return value

    data = {
        name: plain(value)
        for name, value in sorted(globals().items())
        if name.isupper() and not name.startswith("_")
    }
    return json.dumps(data, indent=2, sort_keys=True) + "\n"
