"""Tests for agent/runtime/rm_agent/state.py (tasks.md 2.1): the run store and the state
machine's writes.

moto stands in for DynamoDB: dummy credentials, us-east-1, nothing leaves the process. moto
applies a conditional write, or a whole TransactWriteItems, all or nothing, as DynamoDB does,
but it never reports a transaction conflict. Where DynamoDB would (Amazon DynamoDB Developer
Guide, "Transaction conflict handling in DynamoDB"), a test injects the error the service
documents, sometimes with the colliding write landing first.

Every stored run item is checked against contracts/schemas/run_item.json as it is read.
"""

from __future__ import annotations

import os
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import boto3
import pytest
from boto3.dynamodb.types import TypeDeserializer
from botocore.exceptions import ClientError
from moto import mock_aws
from rm_agent import state

import contracts
from contracts import names
from contracts.dynamo import from_dynamo

START = datetime(2026, 11, 27, 15, 30, 5, tzinfo=UTC)
PROMPT = "Test moving the support assistant from Claude Fable 5.1 to Claude Sonnet 5."
REAL_CLIENT = state._client  # kept before any test replaces it

PLANNED = names.STATE_PLANNED
RUNNING = names.STATE_RUNNING
ROLLED_BACK = names.STATE_ROLLED_BACK
AWAITING_RESUME = names.STATE_AWAITING_RESUME
RESULTS_READY = names.STATE_RESULTS_READY
PROMOTED = names.STATE_PROMOTED
HELD = names.STATE_HELD
ABORTED = names.STATE_ABORTED

ROWS = names.TRANSITIONS
START_ROW = next(t for t in ROWS if t.source is None)
CLAIMED_ROWS = [t for t in ROWS if t.source is not None]
TERMINAL_ROWS = [t for t in CLAIMED_ROWS if t.target in names.TERMINAL_STATES]
ILLEGAL_PAIRS = [
    (source, target)
    for source in (None, *names.STATES)
    for target in names.STATES
    if (source, target) not in names.LEGAL_TRANSITIONS
]


def row(source: str | None, target: str) -> names.Transition:
    return next(t for t in ROWS if (t.source, t.target) == (source, target))


LAUNCH = row(PLANNED, RUNNING)


def row_id(t: names.Transition) -> str:
    return f"{t.source or 'none'}-{t.target}"


def path(source: str, target: str) -> list[names.Transition]:
    """The shortest run of table rows from source to target (breadth first)."""
    routes = {source: []}
    queue = deque([source])
    while queue:
        here = queue.popleft()
        if here == target:
            return routes[here]
        for t in CLAIMED_ROWS:
            if t.source == here and t.target not in routes:
                routes[t.target] = [*routes[here], t]
                queue.append(t.target)
    raise AssertionError(f"no route from {source} to {target}")


def make_plan() -> dict:
    """A plan as task 2.3's planner makes one: the model's wording, every other value from
    contracts.names."""
    return {
        "hypothesis": "Sonnet 5 answers support questions as helpfully as Fable 5.1, for less.",
        "summary": "Split identities 50/50 between Fable 5.1 and Sonnet 5 and compare.",
        "current_model": names.MODEL_CURRENT,
        "cheaper_model": names.MODEL_CHEAPER,
        "primary_metric": names.PRIMARY_METRIC,
        "guardrail_metrics": list(names.GUARDRAIL_METRICS),
        "split": {variant: names.TREATMENT_PERCENT for variant in names.VARIANTS},
        "min_identities_per_variation": names.MIN_IDENTITIES_PER_VARIATION,
        "stop_rules": {
            "rollback_alarm": names.ALARM_NAME,
            "results_embargo_seconds": names.RESULTS_EMBARGO_SECONDS,
            "promote_requires_experiment_ended": True,
        },
        "guardrails": {
            "max_latency_ratio": names.GUARDRAIL_MAX_LATENCY_RATIO,
            "max_cost_ratio": names.GUARDRAIL_MAX_COST_RATIO,
        },
        "recommendation_rule": {
            "after_incident": names.RECOMMEND_HOLD_RERUN,
            "promote_min_prob_not_worse": names.PROMOTE_MIN_PROB_NOT_WORSE,
            "promote_min_helpful_lift_ci_lower": names.PROMOTE_MIN_HELPFUL_LIFT_CI_LOWER,
        },
    }


# ---------------------------------------------------------------------------
# Fixtures: moto DynamoDB with rm-runs, and a clock the test moves by hand
# ---------------------------------------------------------------------------
_AWS_ENV = (
    "AWS_PROFILE",
    "AWS_DEFAULT_PROFILE",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    "AWS_CONTAINER_CREDENTIALS_FULL_URI",
    "AWS_WEB_IDENTITY_TOKEN_FILE",
    "AWS_ROLE_ARN",
)


@pytest.fixture
def db(monkeypatch):
    """Table rm-runs in moto, and state.py pointed at it. No real credential is reachable."""
    for name in _AWS_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", names.REGION)
    monkeypatch.setenv("AWS_CONFIG_FILE", os.devnull)
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", os.devnull)
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    with mock_aws():
        client = boto3.client("dynamodb", region_name=names.REGION)
        client.create_table(
            TableName=names.RUNS_TABLE,
            KeySchema=[{"AttributeName": names.PARTITION_KEY, "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": names.PARTITION_KEY, "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        monkeypatch.setattr(state, "_client", lambda: client)
        yield client


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def clock(monkeypatch):
    fake = Clock(START)
    monkeypatch.setattr(state, "_now", fake)
    return fake


# ---------------------------------------------------------------------------
# Reading the table directly, and driving runs along the table
# ---------------------------------------------------------------------------
_deserialize = TypeDeserializer().deserialize


def raw(db, pk: str) -> dict | None:
    response = db.get_item(
        TableName=names.RUNS_TABLE,
        Key={names.PARTITION_KEY: {"S": pk}},
        ConsistentRead=True,
    )
    item = response.get("Item")
    return None if item is None else from_dynamo({k: _deserialize(v) for k, v in item.items()})


def stored(db, run_id: str) -> dict:
    """The run item as DynamoDB holds it; it must match contracts/schemas/run_item.json."""
    item = raw(db, names.run_pk(run_id))
    assert item is not None, f"no run item for {run_id}"
    contracts.validate("run_item", item)
    return item


def pointer(db) -> dict | None:
    return raw(db, names.ACTIVE_POINTER_PK)


def drop_pointer(db) -> None:
    """What scripts/reset.py does to the pointer (tasks.md 3.5)."""
    db.delete_item(
        TableName=names.RUNS_TABLE,
        Key={names.PARTITION_KEY: {"S": names.ACTIVE_POINTER_PK}},
    )


def reason(t: names.Transition) -> str:
    return f"test: {t.triggers[0]}"


def entry(clock: Clock, t: names.Transition, why: str | None = None) -> dict:
    """The history entry a commit at clock.now writes for row t."""
    return {
        "at": names.utc_iso(clock.now),
        "from": t.source,
        "to": t.target,
        "actor": t.actor,
        "reason": reason(t) if why is None else why,
    }


def step(run_id: str, t: names.Transition, updates: dict | None = None) -> state.Run:
    """Claim and commit one row, as a handler does around its effects."""
    claimed = state.claim(run_id, t.source, t.target)
    return state.commit(
        run_id, t.source, t.target, t.actor, reason(t), updates or {}, claimed.pending_at
    )


def run_in(target: str) -> state.Run:
    """A new run, moved along the table into target."""
    run = state.create_run(PROMPT, make_plan())
    for t in path(run.state, target):
        run = step(run.run_id, t)
    assert run.state == target
    return run


# ---------------------------------------------------------------------------
# Fault injection: what moto cannot produce by itself
# ---------------------------------------------------------------------------
class Interfere:
    """The moto client, except that the next `times` calls of one operation first let a
    colliding write land (if one is given), then fail with the error DynamoDB documents."""

    def __init__(self, client, operation: str, error, before=None, times: int = 1) -> None:
        self._client = client
        self._operation = operation
        self._error = error
        self._before = before
        self._left = times
        self.calls = 0

    def __getattr__(self, name):
        real = getattr(self._client, name)
        if name != self._operation:
            return real

        def call(**kwargs):
            self.calls += 1
            if self._left:
                self._left -= 1
                if self._before:
                    self._before()
                raise self._error()
            return real(**kwargs)

        return call


def cancelled(db, *codes: str) -> ClientError:
    """TransactionCanceledException as botocore raises it: one reason per item, in order."""
    messages = {
        "TransactionConflict": "Transaction is ongoing for the item",
        "ConditionalCheckFailed": "The conditional request failed",
    }
    reasons = [{"Code": c} if c == "None" else {"Code": c, "Message": messages[c]} for c in codes]
    response = {
        "Error": {
            "Code": "TransactionCanceledException",
            "Message": "Transaction cancelled, please refer cancellation reasons for specific "
            f"reasons [{', '.join(codes)}]",
        },
        "CancellationReasons": reasons,
    }
    return db.exceptions.TransactionCanceledException(response, "TransactWriteItems")


def conflict(db) -> ClientError:
    """TransactionConflictException: a single-item write met an in-flight transaction."""
    response = {
        "Error": {
            "Code": "TransactionConflictException",
            "Message": "Transaction is ongoing for the item",
        }
    }
    return db.exceptions.TransactionConflictException(response, "UpdateItem")


class Serialized:
    """The moto client taking one request at a time, as DynamoDB applies the conditional
    writes to one item one after another. The callers still run in parallel threads."""

    def __init__(self, client) -> None:
        self._client = client
        self._lock = threading.Lock()

    def __getattr__(self, name):
        real = getattr(self._client, name)
        if not callable(real):
            return real

        def call(*args, **kwargs):
            with self._lock:
                return real(*args, **kwargs)

        return call


class Recorder:
    """The moto client, keeping every write request it is sent."""

    WRITES = ("put_item", "update_item", "delete_item", "transact_write_items", "batch_write_item")

    def __init__(self, client) -> None:
        self._client = client
        self.writes: list[tuple[str, dict]] = []

    def __getattr__(self, name):
        real = getattr(self._client, name)
        if name not in self.WRITES:
            return real

        def call(**kwargs):
            self.writes.append((name, kwargs))
            return real(**kwargs)

        return call


def keys_written(operation: str, request: dict) -> set[str]:
    """The partition keys one write request touches."""
    if operation == "transact_write_items":
        keys = set()
        for action in request["TransactItems"]:
            ((kind, body),) = action.items()
            key = body["Item"] if kind == "Put" else body["Key"]
            keys.add(key[names.PARTITION_KEY]["S"])
        return keys
    key = request["Item"] if operation == "put_item" else request["Key"]
    return {key[names.PARTITION_KEY]["S"]}


# ---------------------------------------------------------------------------
# Every row of the transition table claims and commits
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("t", ROWS, ids=row_id)
def test_every_row_claims_and_commits(db, clock, t):
    if t.source is None:  # the (none) row is create_run's one transaction
        run = state.create_run(PROMPT, make_plan())
        item = stored(db, run.run_id)
        assert run.item == item
        assert run.run_id == names.run_id_for(START)
        assert (item["state"], item["prompt"], item["plan"]) == (t.target, PROMPT, make_plan())
        assert item["notified"] == {}
        assert item["history"] == [entry(clock, t, names.ACTION_START)]
        assert item["created_at"] == item["updated_at"] == names.utc_iso(START)
        assert "pending" not in item
        assert state.get_active() == state.Pointer(run.run_id, None)
        return

    run = run_in(t.source)
    before = stored(db, run.run_id)
    active_before = state.get_active()

    clock.advance(30)
    claimed = state.claim(run.run_id, t.source, t.target)
    item = stored(db, run.run_id)
    assert claimed.item == item
    assert (item["state"], item["pending"]) == (t.source, t.target)
    assert item["pending_at"] == item["updated_at"] == names.utc_iso(clock.now)
    assert item["history"] == before["history"]  # a claim records nothing
    assert state.get_active() == active_before  # nor touches the pointer

    clock.advance(30)
    committed = state.commit(
        run.run_id, t.source, t.target, t.actor, reason(t), {}, claimed.pending_at
    )
    item = stored(db, run.run_id)
    assert committed.item == item
    assert item["state"] == t.target and "pending" not in item
    assert item["history"] == [*before["history"], entry(clock, t)]
    if t.target in names.TERMINAL_STATES:  # clear_active, in the same transaction
        assert state.get_active() is None
    elif t == LAUNCH:  # the launch commit carries the feature key to the pointer
        feature_key = names.experiment_feature_key(run.run_id)
        assert item["feature_key"] == feature_key
        assert state.get_active() == state.Pointer(run.run_id, feature_key)
    else:
        assert state.get_active() == active_before


@pytest.mark.parametrize("source, target", ILLEGAL_PAIRS, ids=lambda v: v or "none")
def test_every_other_pair_raises_illegal_transition(db, clock, source, target):
    run_id = run_in(source).run_id if source else names.run_id_for(START)
    before = raw(db, names.run_pk(run_id)), pointer(db)
    stale = names.utc_iso(clock.now - timedelta(hours=1))
    attempts = {
        "claim": lambda: state.claim(run_id, source, target),
        "resume": lambda: state.resume(run_id, source, target, stale),
        "commit": lambda: state.commit(
            run_id, source, target, names.ACTOR_AGENT, "test", {}, names.utc_iso(clock.now)
        ),
    }
    for name, attempt in attempts.items():
        with pytest.raises(state.IllegalTransition) as refused:
            attempt()
        assert refused.value.code == names.ERROR_ILLEGAL_IN_STATE, name
    assert (raw(db, names.run_pk(run_id)), pointer(db)) == before  # nothing changed


def test_the_none_row_is_written_only_by_create_run(db, clock):
    run_id = names.run_id_for(START)
    t = START_ROW
    for attempt in (
        lambda: state.claim(run_id, t.source, t.target),
        lambda: state.resume(run_id, t.source, t.target, names.utc_iso(START)),
        lambda: state.commit(run_id, t.source, t.target, t.actor, "start", {}, "x"),
    ):
        with pytest.raises(state.IllegalTransition):
            attempt()
    assert raw(db, names.run_pk(run_id)) is None and pointer(db) is None


# ---------------------------------------------------------------------------
# Races: one winner
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "first, second",
    [(ROLLED_BACK, RESULTS_READY), (RESULTS_READY, ROLLED_BACK), (ROLLED_BACK, ROLLED_BACK)],
)
def test_two_concurrent_claims_from_the_same_state_one_wins(db, clock, first, second):
    """An alarm and a tick that both read the run in RUNNING, then both claim
    (experiment-agent spec, "Two callers race")."""
    run = run_in(RUNNING)
    seen_by_both = run.state

    won = state.claim(run.run_id, seen_by_both, first)
    after_win = stored(db, run.run_id)
    with pytest.raises(state.StaleState) as lost:
        state.claim(run.run_id, seen_by_both, second)
    assert lost.value.run.item == after_win  # the loser gets a fresh read, and changes nothing
    assert stored(db, run.run_id) == after_win
    assert (after_win["pending"], after_win["pending_at"]) == (first, won.pending_at)

    t = row(seen_by_both, first)
    done = state.commit(run.run_id, t.source, t.target, t.actor, reason(t), {}, won.pending_at)
    assert done.state == first
    assert len(done.history) == len(run.history) + 1  # exactly one transition applied


def test_two_claims_from_parallel_threads_one_wins(db, clock, monkeypatch):
    run = run_in(RUNNING)
    serialized = Serialized(db)
    monkeypatch.setattr(state, "_client", lambda: serialized)
    barrier = threading.Barrier(2)

    def claim(target):
        barrier.wait()
        try:
            return state.claim(run.run_id, RUNNING, target)
        except state.StaleState as lost:
            return lost

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(claim, (ROLLED_BACK, RESULTS_READY)))
    winners = [o for o in outcomes if isinstance(o, state.Run)]
    losers = [o for o in outcomes if isinstance(o, state.StaleState)]
    assert len(winners) == 1 and len(losers) == 1
    assert stored(db, run.run_id)["pending"] == winners[0].pending == losers[0].run.pending


# ---------------------------------------------------------------------------
# Takeover (resume) and release
# ---------------------------------------------------------------------------
def test_resume_refuses_a_pending_younger_than_360_seconds(db, clock):
    """Younger is refused, and so is exactly 360 s: design.md lets a pending "older than
    360 seconds" be taken over."""
    run = run_in(RUNNING)
    claimed = state.claim(run.run_id, RUNNING, ROLLED_BACK)
    before = stored(db, run.run_id)
    stale = names.PENDING_STALE_SECONDS
    for age in (0, 1, stale - 1, stale - 0.000001, stale):
        clock.now = datetime.fromisoformat(claimed.pending_at) + timedelta(seconds=age)
        with pytest.raises(state.StaleState) as refused:
            state.resume(run.run_id, RUNNING, ROLLED_BACK, claimed.pending_at)
        assert refused.value.run.item == before
        assert stored(db, run.run_id) == before


def test_resume_takes_over_a_pending_older_than_360_seconds(db, clock):
    run = run_in(RUNNING)
    claimed = state.claim(run.run_id, RUNNING, ROLLED_BACK)
    clock.advance(names.PENDING_STALE_SECONDS + 0.000001)
    taken = state.resume(run.run_id, RUNNING, ROLLED_BACK, claimed.pending_at)
    item = stored(db, run.run_id)
    assert taken.item == item
    assert (item["state"], item["pending"]) == (RUNNING, ROLLED_BACK)
    assert item["pending_at"] == names.utc_iso(clock.now) != claimed.pending_at
    assert item["history"] == run.history


def test_two_takeovers_one_winner_and_the_stalled_owner_cannot_commit(db, clock):
    run = run_in(RUNNING)
    t = row(RUNNING, ROLLED_BACK)
    stalled = state.claim(run.run_id, t.source, t.target)  # then its invocation stalls
    clock.advance(names.PENDING_STALE_SECONDS + 60)
    seen = stored(db, run.run_id)["pending_at"]  # what both later invocations read

    winner = state.resume(run.run_id, t.source, t.target, seen)
    with pytest.raises(state.StaleState) as lost:
        state.resume(run.run_id, t.source, t.target, seen)
    assert lost.value.run.pending_at == winner.pending_at

    with pytest.raises(state.StaleState):  # the stalled owner wakes up and tries to commit
        state.commit(run.run_id, t.source, t.target, t.actor, reason(t), {}, stalled.pending_at)
    item = stored(db, run.run_id)
    assert (item["state"], item["pending_at"]) == (RUNNING, winner.pending_at)
    assert item["history"] == run.history

    done = state.commit(run.run_id, t.source, t.target, t.actor, reason(t), {}, winner.pending_at)
    assert done.state == ROLLED_BACK
    assert len(done.history) == len(run.history) + 1


@pytest.mark.parametrize("effect_seconds", [4, 0.000001], ids=["4 s", "1 us"])
def test_a_released_claim_is_taken_over_at_once(db, clock, effect_seconds):
    """An effect threw after effect_seconds: the claim moves back 360 s, and the very next
    tick takes it over, with no waiting. However little time the effect took, the taker's
    pending_at is later than the released claim's, so the releaser cannot touch it again."""
    run = run_in(RUNNING)
    t = row(RUNNING, ROLLED_BACK)
    claimed = state.claim(run.run_id, t.source, t.target)
    clock.advance(effect_seconds)

    assert state.release(run.run_id, claimed.pending_at) is True
    item = stored(db, run.run_id)
    moved_back = datetime.fromisoformat(claimed.pending_at) - timedelta(
        seconds=names.PENDING_STALE_SECONDS
    )
    assert item["pending_at"] == names.utc_iso(moved_back)
    assert (item["state"], item["pending"], item["history"]) == (RUNNING, t.target, run.history)

    taken = state.resume(run.run_id, t.source, t.target, item["pending_at"])
    assert taken.pending_at == names.utc_iso(clock.now)

    assert state.release(run.run_id, claimed.pending_at) is False  # no longer its claim
    with pytest.raises(state.StaleState):
        state.commit(run.run_id, t.source, t.target, t.actor, reason(t), {}, claimed.pending_at)
    done = state.commit(run.run_id, t.source, t.target, t.actor, reason(t), {}, taken.pending_at)
    assert done.state == ROLLED_BACK
    assert len(done.history) == len(run.history) + 1


def test_release_changes_nothing_once_the_claim_is_not_the_callers(db, clock):
    run = run_in(RUNNING)
    t = row(RUNNING, RESULTS_READY)
    claimed = state.claim(run.run_id, t.source, t.target)
    state.commit(run.run_id, t.source, t.target, t.actor, reason(t), {}, claimed.pending_at)
    after_commit = stored(db, run.run_id)
    assert after_commit["pending_at"] == claimed.pending_at  # a commit leaves it behind
    assert state.release(run.run_id, claimed.pending_at) is False
    assert stored(db, run.run_id) == after_commit


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------
def test_history_grows_by_exactly_one_per_transition(db, clock):
    """Every non-terminal row in one run, ending in PROMOTED. Claims, takeovers and releases
    add nothing; each commit adds one entry."""
    walk = [
        (PLANNED, RUNNING),
        (RUNNING, ROLLED_BACK),
        (ROLLED_BACK, AWAITING_RESUME),
        (AWAITING_RESUME, ROLLED_BACK),
        (ROLLED_BACK, AWAITING_RESUME),
        (AWAITING_RESUME, RUNNING),
        (RUNNING, RESULTS_READY),
        (RESULTS_READY, ROLLED_BACK),
        (ROLLED_BACK, AWAITING_RESUME),
        (AWAITING_RESUME, RUNNING),
        (RUNNING, RESULTS_READY),
        (RESULTS_READY, PROMOTED),
    ]
    assert {(s, t) for s, t in walk} == {(t.source, t.target) for t in CLAIMED_ROWS} - {
        (t.source, t.target) for t in TERMINAL_ROWS if t.target != PROMOTED
    }
    run = state.create_run(PROMPT, make_plan())
    expected = [entry(clock, START_ROW, names.ACTION_START)]
    assert stored(db, run.run_id)["history"] == expected

    for number, (source, target) in enumerate(walk):
        t = row(source, target)
        clock.advance(60)
        claimed = state.claim(run.run_id, source, target)
        assert stored(db, run.run_id)["history"] == expected
        if number % 3 == 1:  # now and then an effect fails and the next tick takes over
            clock.advance(2)
            assert state.release(run.run_id, claimed.pending_at)
            claimed = state.resume(run.run_id, source, target, stored(db, run.run_id)["pending_at"])
            assert stored(db, run.run_id)["history"] == expected
        clock.advance(5)
        state.commit(run.run_id, source, target, t.actor, reason(t), {}, claimed.pending_at)
        expected.append(entry(clock, t))
        assert stored(db, run.run_id)["history"] == expected

    assert [h["to"] for h in expected] == [PLANNED] + [target for _, target in walk]


# ---------------------------------------------------------------------------
# create_run and the pointer
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("later", [5, 60], ids=["same minute", "next minute"])
def test_create_run_with_a_pointer_present_raises_run_active_naming_it(db, clock, later):
    first = state.create_run(PROMPT, make_plan())
    before = stored(db, first.run_id), pointer(db)
    clock.advance(later)
    with pytest.raises(state.RunActive) as refused:
        state.create_run("Move the assistant to Sonnet 5 again.", make_plan())
    assert refused.value.code == names.ERROR_RUN_ACTIVE
    assert refused.value.active_run_id == first.run_id
    assert first.run_id in refused.value.message
    assert (stored(db, first.run_id), pointer(db)) == before
    if names.run_id_for(clock.now) != first.run_id:
        assert raw(db, names.run_pk(names.run_id_for(clock.now))) is None  # no run created


def test_create_run_with_the_run_item_present_raises_bad_request(db, clock):
    """experiment-agent spec, "Restart within the same minute": start, decline, start."""
    first = state.create_run(PROMPT, make_plan())
    clock.advance(10)
    step(first.run_id, row(PLANNED, ABORTED))  # decline clears the pointer
    declined = stored(db, first.run_id)
    assert pointer(db) is None

    clock.advance(20)  # still the same UTC minute, so the same run ID
    with pytest.raises(state.BadRequest) as refused:
        state.create_run(PROMPT, make_plan())
    assert refused.value.code == names.ERROR_BAD_REQUEST
    assert "start again next minute" in refused.value.message
    assert stored(db, first.run_id) == declined and pointer(db) is None  # wrote nothing

    clock.advance(60)
    second = state.create_run(PROMPT, make_plan())
    assert second.run_id == names.run_id_for(clock.now) != first.run_id
    assert state.get_active() == state.Pointer(second.run_id, None)


@pytest.mark.parametrize("t", TERMINAL_ROWS, ids=row_id)
def test_a_terminal_commit_while_the_pointer_names_another_run_is_cancelled(db, clock, t):
    run = run_in(t.source)
    claimed = state.claim(run.run_id, t.source, t.target)
    drop_pointer(db)  # scripts/reset.py --force, then a new start
    clock.advance(60)
    other = state.create_run(PROMPT, make_plan())
    before = stored(db, run.run_id), pointer(db)

    with pytest.raises(state.CommitFailed) as failed:
        state.commit(run.run_id, t.source, t.target, t.actor, reason(t), {}, claimed.pending_at)
    assert failed.value.code == names.ERROR_EFFECT_FAILED
    assert other.run_id in failed.value.message
    assert names.CLEAR_ACTIVE in failed.value.message
    assert (stored(db, run.run_id), pointer(db)) == before  # cancelled: nothing changed


@pytest.mark.parametrize("t", TERMINAL_ROWS, ids=row_id)
def test_a_terminal_commit_with_no_pointer_succeeds(db, clock, t):
    run = run_in(t.source)
    claimed = state.claim(run.run_id, t.source, t.target)
    drop_pointer(db)
    done = state.commit(run.run_id, t.source, t.target, t.actor, reason(t), {}, claimed.pending_at)
    assert done.state == t.target == stored(db, run.run_id)["state"]
    assert pointer(db) is None


@pytest.mark.parametrize("someone_else", [False, True], ids=["no pointer", "another run"])
def test_a_launch_commit_needs_the_pointer_to_name_its_run(db, clock, someone_else):
    """The storage half of "a PLANNED run with no pointer can never launch"."""
    run = state.create_run(PROMPT, make_plan())
    claimed = state.claim(run.run_id, PLANNED, RUNNING)
    drop_pointer(db)
    if someone_else:
        clock.advance(60)
        other = state.create_run(PROMPT, make_plan())
    before = stored(db, run.run_id), pointer(db)

    with pytest.raises(state.CommitFailed) as failed:
        state.commit(run.run_id, PLANNED, RUNNING, LAUNCH.actor, "approve", {}, claimed.pending_at)
    assert failed.value.code == names.ERROR_EFFECT_FAILED
    if someone_else:
        assert other.run_id in failed.value.message
    assert (stored(db, run.run_id), pointer(db)) == before


def test_every_pointer_change_is_inside_the_run_item_writes_transaction(db, clock, monkeypatch):
    recorder = Recorder(db)
    monkeypatch.setattr(state, "_client", lambda: recorder)
    run = run_in(AWAITING_RESUME)
    for t in (row(AWAITING_RESUME, RUNNING), row(RUNNING, RESULTS_READY)):
        step(run.run_id, t)
    claimed = state.claim(run.run_id, RESULTS_READY, HELD)
    clock.advance(2)
    state.release(run.run_id, claimed.pending_at)
    taken = state.resume(run.run_id, RESULTS_READY, HELD, stored(db, run.run_id)["pending_at"])
    t = row(RESULTS_READY, HELD)
    state.commit(run.run_id, t.source, t.target, t.actor, reason(t), {}, taken.pending_at)

    touching = [
        (operation, request)
        for operation, request in recorder.writes
        if names.ACTIVE_POINTER_PK in keys_written(operation, request)
    ]
    assert len(touching) == 3  # create, launch commit, terminal commit
    for operation, request in touching:
        assert operation == "transact_write_items"
        assert keys_written(operation, request) == {
            names.ACTIVE_POINTER_PK,
            names.run_pk(run.run_id),
        }


# ---------------------------------------------------------------------------
# Lost races that moto cannot stage: a colliding transaction, and a claim that meets an
# in-flight commit
# ---------------------------------------------------------------------------
def test_a_colliding_transaction_is_a_lost_race_and_the_commit_retries_once(db, clock, monkeypatch):
    """A start's transaction on the pointer collides with a terminal commit. Re-read: the
    claim is still this invocation's, so the commit is tried once more and lands."""
    run = run_in(RUNNING)
    t = row(RUNNING, HELD)
    claimed = state.claim(run.run_id, t.source, t.target)
    flaky = Interfere(
        db, "transact_write_items", lambda: cancelled(db, "None", "TransactionConflict")
    )
    monkeypatch.setattr(state, "_client", lambda: flaky)

    done = state.commit(run.run_id, t.source, t.target, t.actor, reason(t), {}, claimed.pending_at)
    assert flaky.calls == 2  # the first try and exactly one retry
    assert done.state == HELD
    assert stored(db, run.run_id)["history"] == [*run.history, entry(clock, t)]
    assert pointer(db) is None


def test_a_colliding_transaction_after_which_the_claim_is_lost_is_not_retried(
    db, clock, monkeypatch
):
    run = run_in(RUNNING)
    t = row(RUNNING, ROLLED_BACK)
    stalled = state.claim(run.run_id, t.source, t.target)
    clock.advance(names.PENDING_STALE_SECONDS + 1)
    taken = {}

    def a_tick_takes_over():
        taken["run"] = state.resume(run.run_id, t.source, t.target, stalled.pending_at)

    flaky = Interfere(
        db,
        "transact_write_items",
        lambda: cancelled(db, "TransactionConflict"),
        before=a_tick_takes_over,
    )
    monkeypatch.setattr(state, "_client", lambda: flaky)

    with pytest.raises(state.StaleState) as lost:
        state.commit(run.run_id, t.source, t.target, t.actor, reason(t), {}, stalled.pending_at)
    assert flaky.calls == 1  # the re-read showed the claim is someone else's: no retry
    assert lost.value.run.pending_at == taken["run"].pending_at
    assert stored(db, run.run_id)["history"] == run.history


def test_a_second_colliding_transaction_leaves_the_claim_for_a_later_tick(db, clock, monkeypatch):
    run = run_in(RUNNING)
    t = row(RUNNING, ROLLED_BACK)
    claimed = state.claim(run.run_id, t.source, t.target)
    flaky = Interfere(
        db, "transact_write_items", lambda: cancelled(db, "TransactionConflict"), times=2
    )
    monkeypatch.setattr(state, "_client", lambda: flaky)

    with pytest.raises(state.StaleState) as lost:
        state.commit(run.run_id, t.source, t.target, t.actor, reason(t), {}, claimed.pending_at)
    assert flaky.calls == 2  # retried once, not twice
    item = stored(db, run.run_id)
    assert (item["state"], item["pending"], item["pending_at"]) == (
        RUNNING,
        ROLLED_BACK,
        claimed.pending_at,
    )
    assert lost.value.run.item == item


def test_a_claim_that_meets_an_in_flight_commit_is_a_lost_race(db, clock, monkeypatch):
    """A tick read the run in RUNNING. Meanwhile an alarm claimed RUNNING -> ROLLED_BACK and
    is committing, so the tick's claim meets that transaction: TransactionConflictException."""
    run = run_in(RUNNING)
    t = row(RUNNING, ROLLED_BACK)
    alarm = state.claim(run.run_id, t.source, t.target)

    def the_alarm_commits():
        state.commit(run.run_id, t.source, t.target, t.actor, reason(t), {}, alarm.pending_at)

    flaky = Interfere(db, "update_item", lambda: conflict(db), before=the_alarm_commits)
    monkeypatch.setattr(state, "_client", lambda: flaky)

    with pytest.raises(state.StaleState) as lost:
        state.claim(run.run_id, RUNNING, RESULTS_READY)  # the tick
    assert (lost.value.run.state, lost.value.run.pending) == (ROLLED_BACK, None)
    assert stored(db, run.run_id)["history"] == [*run.history, entry(clock, t)]  # one applied


def test_create_run_that_collides_with_another_start_names_the_winner(db, clock, monkeypatch):
    winner = {}

    def another_start_lands():
        winner["run"] = state.create_run(PROMPT, make_plan())

    flaky = Interfere(
        db,
        "transact_write_items",
        lambda: cancelled(db, "TransactionConflict", "TransactionConflict"),
        before=another_start_lands,
    )
    monkeypatch.setattr(state, "_client", lambda: flaky)

    with pytest.raises(state.RunActive) as refused:
        state.create_run(PROMPT, make_plan())
    assert refused.value.active_run_id == winner["run"].run_id
    assert stored(db, winner["run"].run_id) == winner["run"].item  # only the winner wrote


def test_create_run_that_collides_with_nothing_found_fails_as_it_came(db, clock, monkeypatch):
    """Nobody holds the pointer after the collision, so there is no refusal to name: the
    error goes back unchanged (the handler's UPSTREAM_FAILED), and nothing was written."""
    flaky = Interfere(
        db, "transact_write_items", lambda: cancelled(db, "TransactionConflict", "None")
    )
    monkeypatch.setattr(state, "_client", lambda: flaky)
    with pytest.raises(ClientError) as failed:
        state.create_run(PROMPT, make_plan())
    assert failed.value.response["Error"]["Code"] == "TransactionCanceledException"
    assert raw(db, names.run_pk(names.run_id_for(START))) is None and pointer(db) is None


# ---------------------------------------------------------------------------
# What a commit writes, and what it refuses
# ---------------------------------------------------------------------------
def test_the_launch_commit_writes_its_updates_with_the_feature_key(db, clock):
    run = state.create_run(PROMPT, make_plan())
    claimed = state.claim(run.run_id, PLANNED, RUNNING)
    feature_key = names.experiment_feature_key(run.run_id)
    updates = {
        "feature_key": feature_key,
        "experiment_id": 4321,
        "metric_ids": {metric: n for n, metric in enumerate(names.METRICS, start=7)},
        "first_running_at": names.utc_iso(clock.now),
    }
    done = state.commit(
        run.run_id, PLANNED, RUNNING, LAUNCH.actor, "approve", updates, claimed.pending_at
    )
    item = stored(db, run.run_id)
    assert done.item == item
    assert {key: item[key] for key in updates} == updates
    assert state.get_active() == state.Pointer(run.run_id, feature_key)


def test_a_launch_commit_with_another_runs_feature_key_is_refused(db, clock):
    run = state.create_run(PROMPT, make_plan())
    claimed = state.claim(run.run_id, PLANNED, RUNNING)
    before = stored(db, run.run_id), pointer(db)
    wrong = {"feature_key": names.experiment_feature_key("rother0000")}
    with pytest.raises(ValueError):
        state.commit(run.run_id, PLANNED, RUNNING, LAUNCH.actor, "x", wrong, claimed.pending_at)
    assert (stored(db, run.run_id), pointer(db)) == before


def test_a_commit_records_the_tables_actor_and_refuses_another(db, clock):
    """approve in AWAITING_RESUME with a failing probe: the engineer's action, the agent's
    transition (design.md section 6; run_item.json: the table's actor, not the payload's)."""
    run = run_in(AWAITING_RESUME)
    t = row(AWAITING_RESUME, ROLLED_BACK)
    claimed = state.claim(run.run_id, t.source, t.target)
    before = stored(db, run.run_id)
    with pytest.raises(ValueError):
        state.commit(
            run.run_id, t.source, t.target, names.ACTOR_ENGINEER, "x", {}, claimed.pending_at
        )
    assert stored(db, run.run_id) == before
    done = state.commit(run.run_id, t.source, t.target, t.actor, reason(t), {}, claimed.pending_at)
    assert done.history[-1]["actor"] == names.ACTOR_AGENT


@pytest.mark.parametrize(
    "updates",
    [
        {"state": HELD},
        {"pending": None},
        {"pending_at": "2026-11-27T15:30:05.000000Z"},
        {"history": []},
        {"updated_at": "2026-11-27T15:30:05.000000Z"},
        {"prompt": "something else"},
        {"plan": {}},
        {"created_at": "2026-11-27T15:30:05.000000Z"},
        {"run_id": "rother0000"},
        {names.PARTITION_KEY: names.ACTIVE_POINTER_PK},
        {"verdict": "promote"},
        {"report_key": None},
    ],
    ids=lambda updates: next(iter(updates)),
)
def test_a_commit_refuses_updates_it_does_not_own(db, clock, updates):
    run = run_in(RUNNING)
    t = row(RUNNING, RESULTS_READY)
    claimed = state.claim(run.run_id, t.source, t.target)
    before = stored(db, run.run_id), pointer(db)
    with pytest.raises(ValueError):
        state.commit(
            run.run_id, t.source, t.target, t.actor, reason(t), updates, claimed.pending_at
        )
    assert (stored(db, run.run_id), pointer(db)) == before


# ---------------------------------------------------------------------------
# Reads, refusals and wiring
# ---------------------------------------------------------------------------
def test_reads_of_a_run_that_does_not_exist(db, clock):
    run_id = names.run_id_for(START)
    assert state.get_active() is None
    with pytest.raises(state.NotFound) as missing:
        state.load_run(run_id)
    assert missing.value.code == names.ERROR_NOT_FOUND
    with pytest.raises(state.NotFound):
        state.claim(run_id, PLANNED, RUNNING)
    assert raw(db, names.run_pk(run_id)) is None  # the failed claim created nothing


def test_load_run_returns_what_is_stored(db, clock):
    run = run_in(ROLLED_BACK)
    loaded = state.load_run(run.run_id)
    assert loaded == run
    assert loaded.item == stored(db, run.run_id)
    assert (loaded.run_id, loaded.state, loaded.pending, loaded.history) == (
        run.run_id,
        ROLLED_BACK,
        None,
        run.history,
    )


def test_the_shapes_come_from_the_contract():
    assert state.Pointer._fields == names.POINTER_ATTRIBUTES
    assert state.RunActive.code == names.ERROR_RUN_ACTIVE
    assert state.BadRequest.code == names.ERROR_BAD_REQUEST
    assert state.NotFound.code == names.ERROR_NOT_FOUND
    assert state.IllegalTransition.code == names.ERROR_ILLEGAL_IN_STATE
    assert state.CommitFailed.code == names.ERROR_EFFECT_FAILED


def test_the_default_client_is_dynamodb_in_us_east_1(db):
    REAL_CLIENT.cache_clear()
    try:
        client = REAL_CLIENT()
        assert client.meta.region_name == names.REGION
        assert client.meta.service_model.service_name == "dynamodb"
        assert REAL_CLIENT() is client  # made once
    finally:
        REAL_CLIENT.cache_clear()
