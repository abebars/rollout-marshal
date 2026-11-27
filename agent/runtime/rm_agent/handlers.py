"""rm_agent's action handlers: handle(payload, tools) -> response (tasks.md 2.5; design.md
sections 6 and 7).

Every action in contracts/schemas/invoke.json, answered with a response.json object. A
transition is applied as design.md section 6 gives it: the guard (reads that change nothing),
then the claim through state.py, then the effects in order, then the commit. A claim older
than PENDING_STALE_SECONDS is taken over and its effects run again from the start, so every
effect is idempotent. An effect that throws releases the claim, so the next tick resumes it at
once. A failure before any claim changes nothing and is UPSTREAM_FAILED.

The model writes three things only: the plan (2.3's make_plan), the incident note, and the
report prose. The report prose is written in the RUNNING -> RESULTS_READY guard, before the
claim (inside the effect when a takeover resumes that transition). The incident note is
written inside the rollback's effects, after the kill switch is off; if the model fails, the
note goes out with the facts alone, so a model failure never holds up a rollback's notice.

Every tool arrives in Tools, so the tests can pass fakes; main.py builds the real ones.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from contracts import names
from rm_agent import planner, state
from rm_agent.tools import flagsmith, report


@dataclass
class Tools:
    """What the handlers call besides the state store."""

    flagsmith: Any  # tools.flagsmith.Flagsmith
    alarm_state: Callable[[str], Any]  # tools.signals.alarm_state -> Alarm(state, threshold_ms)
    probe_cheaper: Callable[[float], dict]  # tools.probe.probe_cheaper
    set_tick: Callable[[bool], bool]  # tools.schedule.set_tick
    notify: Callable[[str, str, str, str], str]  # tools.notify.notify
    write_report: Callable[..., str]  # tools.report.write_report(run, results, prose)
    make_plan: Callable[[str], dict]  # planner.make_plan
    converse: Callable[[dict], str]  # planner.converse


def _now() -> datetime:
    """The current time in UTC. Tests replace it."""
    return datetime.now(UTC)


_STALE_AFTER = timedelta(seconds=names.PENDING_STALE_SECONDS)
_EMBARGO = timedelta(seconds=names.RESULTS_EMBARGO_SECONDS)
_ENGINEER_ACTIONS = (
    names.ACTION_START,
    names.ACTION_APPROVE,
    names.ACTION_DECLINE,
    names.ACTION_CHECK,
)
_RUN_ACTIONS = (names.ACTION_APPROVE, names.ACTION_DECLINE, names.ACTION_CHECK)
_ROWS = {(t.source, t.target): t for t in names.TRANSITIONS if t.source is not None}
_ROLLBACK_SOURCES = (names.STATE_RUNNING, names.STATE_RESULTS_READY)
_PROSE_SECTION = "Recommendation"  # the report section the model's prose goes under
if _PROSE_SECTION not in names.REPORT_SECTIONS:
    raise KeyError(f"{_PROSE_SECTION!r} is not in contracts.names.REPORT_SECTIONS")
_SHOWN = 300  # characters of an upstream error shown in a message
# invoke.json's timestamp: ISO 8601 with an explicit offset, Z, +HH:MM or +HHMM.
_TIMESTAMP = re.compile(
    r"[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])T([01][0-9]|2[0-3]):[0-5][0-9]:"
    r"[0-5][0-9](\.[0-9]{1,9})?(Z|[+-][0-9]{2}:?[0-9]{2})"
)
_NOTE_SYSTEM = (
    "You write the incident note an engineer reads when Rollout Marshal rolls back an "
    "experiment. In at most 80 words of plain text, no Markdown, say what happened and what "
    "happens next, using only the facts given. Do not add numbers or advice of your own."
)
_REPORT_SYSTEM = (
    "You write one paragraph of plain prose, no Markdown and no headings, at most 120 words, "
    "for a model-migration decision report. Explain the recommendation given, in plain words, "
    "from the facts given. Never change or question the recommendation, and add no numbers "
    "that are not in the facts."
)


# ---------------------------------------------------------------------------
# Payloads and responses
# ---------------------------------------------------------------------------
def _is_run_id(value: Any) -> bool:
    return isinstance(value, str) and names.is_run_id(value)


def is_invoke_payload(payload: Any) -> bool:
    """contracts/schemas/invoke.json, checked by hand: the runtime carries no jsonschema. A
    test checks the two agree."""
    if not isinstance(payload, dict):
        return False
    action, keys = payload.get("action"), set(payload)
    engineer = payload.get("actor") == names.ACTOR_ENGINEER
    if action == names.ACTION_START:
        prompt = payload.get("prompt")
        return (
            keys == {"action", "prompt", "actor"}
            and engineer
            and isinstance(prompt, str)
            and len(prompt) >= 1
        )
    if action in _RUN_ACTIONS:
        return keys == {"action", "run_id", "actor"} and engineer and _is_run_id(payload["run_id"])
    if action == names.ACTION_STATUS:
        return keys <= {"action", "run_id"} and (
            "run_id" not in payload or _is_run_id(payload["run_id"])
        )
    if action == names.ACTION_ALARM:
        at = payload.get("at")
        return (
            keys == {"action", "alarm_name", "alarm_state", "at"}
            and payload["alarm_name"] == names.ALARM_NAME
            and payload["alarm_state"] == names.ALARM_STATE_ALARM
            and isinstance(at, str)
            and _TIMESTAMP.fullmatch(at) is not None
        )
    if action == names.ACTION_TICK:
        return keys == {"action"}
    return False


def _response(run: state.Run | None, message: str, code: str | None = None) -> dict:
    """response.json for a run (or none). With code, the error carries the same message."""
    error = None if code is None else {"code": code, "message": message}
    if run is None:
        return _bare(None, message, code)
    return {
        "run_id": run.run_id,
        "state": run.state,
        "message": message,
        "awaiting": names.awaiting_for(run.state),
        "pending": run.pending,
        "error": error,
    }


def _bare(run_id: str | None, message: str, code: str | None = None) -> dict:
    """response.json naming a run, or none, whose state is not read."""
    return {
        "run_id": run_id,
        "state": None,
        "message": message,
        "awaiting": None,
        "pending": None,
        "error": None if code is None else {"code": code, "message": message},
    }


def _shown(error: BaseException) -> str:
    return f"{type(error).__name__}: {str(error)[:_SHOWN]}"


def _percent(ratio: float) -> int:
    return round(ratio * 100)


def _approval(run: state.Run) -> str:
    """What start answers, and status says of a PLANNED run: the model's summary, and every
    value the planner injected, so the engineer approves the plan itself."""
    plan, rid = run.item["plan"], run.run_id
    split, stops = plan["split"], plan["stop_rules"]
    guardrails, rule = plan["guardrails"], plan["recommendation_rule"]
    latency, cost = plan["guardrail_metrics"]
    return (
        f"Planned run {rid}: {plan['summary']}\n"
        f"Hypothesis: {plan['hypothesis']}\n"
        f"Split: {split[names.VARIANT_CURRENT]} percent {names.VARIANT_CURRENT} "
        f"({plan['current_model']}), {split[names.VARIANT_CHEAPER]} percent "
        f"{names.VARIANT_CHEAPER} ({plan['cheaper_model']}).\n"
        f"Primary metric: {plan['primary_metric']}. Guardrails: the cheaper arm's {latency} at "
        f"most {_percent(guardrails['max_latency_ratio'])} percent of the current arm's, and its "
        f"{cost} at most {_percent(guardrails['max_cost_ratio'])} percent of it.\n"
        f"Floor: at least {plan['min_identities_per_variation']} identities per variation, and "
        f"no results are read in the first {stops['results_embargo_seconds']} seconds.\n"
        f"Alarm: {stops['rollback_alarm']} rolls the run back without asking.\n"
        f"Rule: any incident means {rule['after_incident']}; otherwise promote only if the lower "
        f"bound of Flagsmith's {names.CREDIBLE_INTERVAL_PERCENT} percent credible interval "
        "for the lift on "
        f"{plan['primary_metric']} is at or above "
        f"{_percent(rule['promote_min_helpful_lift_ci_lower'])} percent and both guardrails "
        f"pass; otherwise hold. Promotion also waits for the experiment to be ended in "
        f"Flagsmith, and for your approve.\n"
        f"Approve with `marshal approve {rid}`, or decline with `marshal decline {rid}`."
    )


def _describe(run: state.Run) -> str:
    """The run's state in plain words, and what moves it next."""
    item, rid = run.item, run.run_id
    if run.state == names.STATE_PLANNED:
        text = _approval(run)
    elif run.state == names.STATE_RUNNING:
        text = (
            f"Run {rid} is running: experiment {item.get('experiment_id')} on "
            f"{item.get('feature_key')}. Each tick checks the alarm, then reads results."
        )
    elif run.state == names.STATE_ROLLED_BACK:
        text = (
            f"Run {rid} is rolled back: {names.KILL_SWITCH_FEATURE} is off, so everyone is "
            "served the current model. Each tick probes the cheaper model and asks you to "
            f"resume once it is healthy; `marshal decline {rid}` holds the run."
        )
    elif run.state == names.STATE_AWAITING_RESUME:
        text = (
            f"Run {rid} waits for you: the cheaper model looks healthy again. "
            f"`marshal approve {rid}` resumes the experiment; `marshal decline {rid}` holds it."
        )
    elif run.state == names.STATE_RESULTS_READY:
        text = (
            f"Run {rid} has results: the report is at s3://{names.REPORTS_BUCKET}/"
            f"{item.get('report_key')}. To promote, end the experiment in the Flagsmith "
            f"dashboard, then `marshal approve {rid}`; `marshal decline {rid}` holds it."
        )
    elif run.state == names.STATE_PROMOTED:
        text = (
            f"Run {rid} is promoted: {names.DEFAULT_MODEL_FEATURE} is {names.VARIANT_CHEAPER}, "
            "so everyone is served the cheaper model."
        )
    elif run.state == names.STATE_HELD:
        text = f"Run {rid} is held: it changes no more, and Flagsmith is left as it was."
    else:
        text = f"Run {rid} was declined before launch; nothing was created in Flagsmith."
    if run.pending:
        text += " " + _pending_text(run)
    return text


def _pending_text(run: state.Run) -> str:
    age = (_now() - datetime.fromisoformat(run.pending_at)).total_seconds()
    return (
        f"{run.state} -> {run.pending} has been pending for {age:.0f} s; "
        f"`marshal check {run.run_id}` resumes it once the claim is older than "
        f"{names.PENDING_STALE_SECONDS} s."
    )


def _pending(run: state.Run, engineer: bool, *, reset: bool = False) -> dict:
    """The status of a run whose transition another invocation holds: TRANSITION_PENDING for
    an engineer action, a no-op for tick and alarm."""
    if reset:
        text = (
            f"run {run.run_id} has no pointer, so it was reset while {run.pending} was "
            "pending: start a new one"
        )
    else:
        text = f"Run {run.run_id}: {_pending_text(run)}"
    return _response(run, text, names.ERROR_TRANSITION_PENDING if engineer else None)


# ---------------------------------------------------------------------------
# Transitions: claim or take over, effects in order, commit
# ---------------------------------------------------------------------------
def _stale(run: state.Run) -> bool:
    """A pending claim strictly older than PENDING_STALE_SECONDS may be taken over."""
    return _now() - datetime.fromisoformat(run.pending_at) > _STALE_AFTER


def _row(source: str, target: str) -> names.Transition:
    return _ROWS[(source, target)]


class _Plan:
    """A transition's effects, in order, each with its name, and the commit's updates."""

    def __init__(self) -> None:
        self.effects: list[tuple[str, Callable[[], Any]]] = []
        self.updates: Callable[[], dict] = dict

    def add(self, name: str, effect: Callable[[], Any]) -> None:
        self.effects.append((name, effect))


def _incidents(item: dict) -> list[dict]:
    return [dict(incident) for incident in item.get("incidents") or []]


def _incident_note(tools: Tools, run: state.Run, action: str) -> str:
    """The model's note, then the facts. A model failure leaves the facts alone."""
    rid = run.run_id
    facts = (
        f"Run {rid} rolled back at {names.utc_iso(_now())}, on {action}: the alarm "
        f"{names.ALARM_NAME} is in ALARM. {names.KILL_SWITCH_FEATURE} is now off, so every "
        f"user is served the current model ({names.MODEL_CURRENT}). Each tick probes the "
        "cheaper model and asks you to resume once it is healthy; "
        f"`marshal decline {rid}` holds the run instead."
    )
    try:
        note = tools.converse(planner.agent_request(_NOTE_SYSTEM, facts)).strip()
    except Exception as error:  # noqa: BLE001 - the note must go out whatever the model does
        return f"{facts}\n\n(The model's note could not be written: {type(error).__name__}.)"
    return f"{note}\n\n{facts}" if note else facts


def _report_prose(tools: Tools, run: state.Run, results: dict) -> dict[str, str]:
    """The model's paragraph for the report, under the Recommendation. A model failure
    propagates: in the guard it is UPSTREAM_FAILED, in a resumed effect EFFECT_FAILED."""
    facts = json.dumps(
        {
            "run_id": run.run_id,
            "hypothesis": run.item["plan"]["hypothesis"],
            "results": results,
            "incidents": run.item.get("incidents") or [],
            "recommendation": report.recommend(run, results),
            "rule": run.item["plan"]["recommendation_rule"],
        },
        default=str,
    )
    prose = tools.converse(planner.agent_request(_REPORT_SYSTEM, facts)).strip()
    return {_PROSE_SECTION: prose} if prose else {}


def _plan(tools: Tools, run: state.Run, target: str, action: str, context: dict) -> _Plan:
    """The effects and updates of run.state -> target (design.md section 6), from the claimed
    run. context holds what the guard learned; a takeover starts with none."""
    source, item, rid = run.state, run.item, run.run_id
    # The dedupe key: the history length when claimed, and the target (design.md section 6).
    key = names.dedupe_key(len(run.history), target)
    plan = _Plan()
    fs = tools.flagsmith
    if (source, target) == (names.STATE_PLANNED, names.STATE_RUNNING):
        plan.add("set_tick", lambda: tools.set_tick(True))  # first, so a tick can finish it
        plan.add("ensure_permanent_features", lambda: fs.ensure_permanent_features())
        plan.add("set_kill_switch", lambda: fs.set_kill_switch(True))
        plan.add(
            "ensure_experiment", lambda: context.update(fs.ensure_experiment(rid, item["plan"]))
        )
        plan.add("start_experiment", lambda: fs.start_experiment(context["experiment_id"]))

        def launched() -> dict:
            updates = {
                "feature_key": context["feature_key"],
                "experiment_id": context["experiment_id"],
                "metric_ids": context["metric_ids"],
            }
            if not item.get("first_running_at"):
                updates["first_running_at"] = names.utc_iso(_now())
            return updates

        plan.updates = launched
    elif target == names.STATE_ROLLED_BACK and source in _ROLLBACK_SOURCES:
        plan.add("set_kill_switch", lambda: fs.set_kill_switch(False))
        plan.add(
            "notify",
            lambda: tools.notify(
                rid, f"Rollout Marshal {rid}: rolled back", _incident_note(tools, run, action), key
            ),
        )
        plan.updates = lambda: {
            "incidents": [*_incidents(item), {"started_at": names.utc_iso(_now())}]
        }
    elif (source, target) == (names.STATE_ROLLED_BACK, names.STATE_AWAITING_RESUME):
        body = (
            f"Run {rid}: the cheaper model looks healthy again (the alarm is not in ALARM, and "
            f"{names.PROBE_INVOKES} probes answered under the alarm's threshold). "
            f"`marshal approve {rid}` resumes the experiment; `marshal decline {rid}` holds it."
        )
        plan.add(
            "notify",
            lambda: tools.notify(rid, f"Rollout Marshal {rid}: ready to resume", body, key),
        )
    elif (source, target) == (names.STATE_AWAITING_RESUME, names.STATE_RUNNING):
        plan.add("set_kill_switch", lambda: fs.set_kill_switch(True))

        def resumed() -> dict:
            incidents = _incidents(item)
            for incident in reversed(incidents):
                if not incident.get("resumed_at"):
                    incident["resumed_at"] = names.utc_iso(_now())
                    break
            return {"incidents": incidents} if incidents else {}

        plan.updates = resumed
    elif (source, target) == (names.STATE_AWAITING_RESUME, names.STATE_ROLLED_BACK):
        why = context.get("why") or "the probe failed"
        body = (
            f"Run {rid}: the cheaper model is slow again ({why}), so the run is back in "
            f"{names.STATE_ROLLED_BACK}. {names.KILL_SWITCH_FEATURE} stays off; each tick probes "
            "again."
        )
        plan.add(
            "notify", lambda: tools.notify(rid, f"Rollout Marshal {rid}: slow again", body, key)
        )
    elif (source, target) == (names.STATE_RUNNING, names.STATE_RESULTS_READY):
        if "results" not in context:  # a takeover: read the last results and write the prose
            plan.add(
                "get_results",
                lambda: context.update(
                    results=fs.get_results(item["experiment_id"], item["metric_ids"])
                ),
            )
        if "prose" not in context:
            plan.add(
                "report prose",
                lambda: context.update(prose=_report_prose(tools, run, context["results"])),
            )
        plan.add(
            "write_report",
            lambda: context.update(
                report_key=tools.write_report(run, context["results"], context["prose"])
            ),
        )

        def publish() -> str:
            recommendation = report.recommend(run, context["results"])
            prose = context["prose"].get(_PROSE_SECTION, "")
            body = (
                f"Results are ready for run {rid}. The report recommends {recommendation}: "
                f"s3://{names.REPORTS_BUCKET}/{context['report_key']}\n\n{prose}\n\n"
                "To promote, end the experiment in the Flagsmith dashboard, then "
                f"`marshal approve {rid}`. To hold, `marshal decline {rid}`."
            )
            return tools.notify(rid, f"Rollout Marshal {rid}: results ready", body, key)

        plan.add("notify", publish)
        plan.updates = lambda: {"report_key": context["report_key"]}
    elif (source, target) == (names.STATE_RESULTS_READY, names.STATE_PROMOTED):
        plan.add("set_default_model", lambda: fs.set_default_model(names.VARIANT_CHEAPER))
        plan.add("set_tick", lambda: tools.set_tick(False))  # last
    elif target in (names.STATE_HELD, names.STATE_ABORTED):
        plan.add("set_tick", lambda: tools.set_tick(False))  # Flagsmith is left as it is
    return plan


def _transit(
    tools: Tools, run: state.Run, target: str, action: str, context: dict | None = None
) -> dict:
    """Claim run.state -> target (or take over its stale claim), run the effects, commit."""
    source, rid = run.state, run.run_id
    row = _row(source, target)
    engineer = action in _ENGINEER_ACTIONS
    takeover = run.pending is not None
    try:
        if takeover:
            claimed = state.resume(rid, source, target, run.pending_at)
        else:
            claimed = state.claim(rid, source, target)
    except state.StaleState as lost:
        return _lost(lost.run, engineer)
    claimed_at = claimed.pending_at
    reason = f"{action}, resuming {source} -> {target}" if takeover else action
    step = "preparing the effects"  # the step running, named in an EFFECT_FAILED message
    try:  # from here on the claim is this invocation's: any failure releases it
        plan = _plan(tools, claimed, target, action, dict(context or {}))
        for name, effect in plan.effects:
            step = name
            effect()
        step = "commit"
        committed = state.commit(rid, source, target, row.actor, reason, plan.updates(), claimed_at)
    except state.StaleState as lost:
        return _lost(lost.run, engineer)
    except Exception as error:  # noqa: BLE001 - every effect or commit failure is EFFECT_FAILED
        return _effect_failed(rid, target, step, error, claimed_at)
    return _response(committed, _describe(committed))


def _lost(run: state.Run, engineer: bool) -> dict:
    """Another invocation got there first: its status, TRANSITION_PENDING for an engineer
    action while the transition is still pending."""
    if run.pending:
        return _pending(run, engineer)
    return _response(run, _describe(run))


def _effect_failed(rid: str, target: str, step: str, error: Exception, claimed_at: str) -> dict:
    try:
        released = state.release(rid, claimed_at)
    except Exception:  # noqa: BLE001 - the claim then waits out PENDING_STALE_SECONDS
        released = False
    if released:
        when = (
            f"The claim was released, so the next tick (every {names.TICK_INTERVAL_MINUTES} "
            f"minutes) or `marshal check {rid}` resumes {target} at once."
        )
    else:
        when = (
            f"`marshal check {rid}` resumes {target} once the claim is older than "
            f"{names.PENDING_STALE_SECONDS} s."
        )
    message = f"Run {rid}: {step} failed ({_shown(error)}). {when}"
    try:
        run = state.load_run(rid)
    except Exception:  # noqa: BLE001 - the answer still names the run
        return _bare(rid, message, names.ERROR_EFFECT_FAILED)
    return _response(run, message, names.ERROR_EFFECT_FAILED)


# ---------------------------------------------------------------------------
# Guards: the reads that decide whether to move, before any claim
# ---------------------------------------------------------------------------
_ALARM, _FAILED, _PASSED = "alarm", "failed", "passed"


def _probe(tools: Tools) -> tuple[str, str]:
    """The alarm, then the health probe: (_ALARM, why) when the alarm is in ALARM (the probe
    is skipped), (_FAILED, why) when the probe fails, which a failed alarm read counts as,
    else (_PASSED, why)."""
    try:
        alarm = tools.alarm_state(names.ALARM_NAME)
    except Exception as error:  # noqa: BLE001 - a failed read fails the probe
        return _FAILED, f"the alarm could not be read: {_shown(error)}"
    if alarm.state == names.ALARM_STATE_ALARM:
        return _ALARM, f"{names.ALARM_NAME} is in ALARM"
    try:
        result = tools.probe_cheaper(alarm.threshold_ms)
    except Exception as error:  # noqa: BLE001 - the probe fails, it does not raise
        return _FAILED, f"the probe raised {_shown(error)}"
    return (_PASSED if result.get("passed") else _FAILED), str(result.get("reason"))


def _results(tools: Tools, run: state.Run, action: str) -> dict:
    """RUNNING with the alarm not in ALARM: after the embargo, refresh and read results, and
    move to RESULTS_READY when they are ready, with the report prose written first."""
    item, rid = run.item, run.run_id
    opens = datetime.fromisoformat(item["first_running_at"]) + _EMBARGO
    if _now() < opens:
        return _response(run, f"{_describe(run)} Results are read from {names.utc_iso(opens)} on.")
    experiment_id, metric_ids = item["experiment_id"], item["metric_ids"]
    last = item.get("last_refresh_at")
    refreshed = tools.flagsmith.refresh_results(experiment_id, last)
    if refreshed:
        state.record_refresh(rid, refreshed)
    try:
        results = tools.flagsmith.get_results(
            experiment_id, metric_ids, refreshed_at=refreshed or last
        )
    except flagsmith.ResultsNotCurrent as error:
        return _response(run, f"{_describe(run)} Results are not ready yet: {error}")
    if not results.get("ready"):
        counts = ", ".join(
            f"{variant} {results['variations'][variant]['identities']}"
            for variant in names.VARIANTS
        )
        return _response(
            run,
            f"{_describe(run)} Results are not ready yet: identities {counts}, at least "
            f"{names.MIN_IDENTITIES_PER_VARIATION} each and every mean needed.",
        )
    prose = _report_prose(tools, run, results)
    return _transit(
        tools, run, names.STATE_RESULTS_READY, action, {"results": results, "prose": prose}
    )


def _advance(tools: Tools, run: state.Run, action: str) -> dict:
    """What a tick does for the run's state, and check, which does the same."""
    engineer = action in _ENGINEER_ACTIONS
    if run.pending:
        if _stale(run):  # tick and check may take over any transition
            return _transit(tools, run, run.pending, action)
        return _pending(run, engineer)
    if run.state in _ROLLBACK_SOURCES:  # the alarm first
        alarm = tools.alarm_state(names.ALARM_NAME)
        if alarm.state == names.ALARM_STATE_ALARM:
            return _transit(tools, run, names.STATE_ROLLED_BACK, action)
        if run.state == names.STATE_RUNNING:
            return _results(tools, run, action)
        return _response(run, _describe(run))
    if run.state == names.STATE_ROLLED_BACK:
        outcome, why = _probe(tools)
        if outcome != _PASSED:
            return _response(run, f"{_describe(run)} Not yet: {why}.")
        return _transit(tools, run, names.STATE_AWAITING_RESUME, action, {"why": why})
    return _response(run, _describe(run))  # PLANNED and AWAITING_RESUME wait for the engineer


# ---------------------------------------------------------------------------
# The actions
# ---------------------------------------------------------------------------
def _active_run() -> state.Run | dict:
    """The active run, or the no-op answer when there is none: no pointer, or a pointer
    whose run item is missing."""
    pointer = state.get_active()
    if pointer is None:
        return _bare(None, names.NO_ACTIVE_RUN_MESSAGE)
    try:
        return state.load_run(pointer.run_id)
    except state.NotFound:
        return _bare(
            None,
            f"{names.NO_ACTIVE_RUN_MESSAGE}: the pointer names run {pointer.run_id}, whose run "
            f"item is missing; clear the pointer with {names.RESET_SCRIPT}",
        )


def _start(payload: dict, tools: Tools) -> dict:
    active = state.get_active()
    if active is not None:  # before the model call, which would only be wasted
        return _run_active(active.run_id)
    try:
        plan = tools.make_plan(payload["prompt"])
    except planner.BadRequest as error:
        return _bare(None, error.message, names.ERROR_BAD_REQUEST)
    except planner.PlannerError as error:
        return _bare(None, f"{error.message}; nothing was created", names.ERROR_UPSTREAM_FAILED)
    try:
        run = state.create_run(payload["prompt"], plan)
    except state.RunActive as error:
        return _run_active(error.active_run_id)
    except state.BadRequest as error:
        return _bare(None, error.message, names.ERROR_BAD_REQUEST)
    except Exception as error:  # noqa: BLE001 - a create that collided: nothing was written
        return _bare(
            None,
            f"the create failed ({_shown(error)}); nothing was written",
            names.ERROR_UPSTREAM_FAILED,
        )
    return _response(run, _approval(run))


def _run_active(active_run_id: str) -> dict:
    message = (
        f"run {active_run_id} is active; finish it first (`marshal status`), since only one run "
        "runs at a time"
    )
    try:
        return _response(state.load_run(active_run_id), message, names.ERROR_RUN_ACTIVE)
    except state.NotFound:
        return _bare(active_run_id, message, names.ERROR_RUN_ACTIVE)


def _status(payload: dict, tools: Tools) -> dict:
    run_id = payload.get("run_id")
    if run_id is not None:
        try:
            run = state.load_run(run_id)
        except state.NotFound:
            return _bare(run_id, f"no run {run_id}", names.ERROR_NOT_FOUND)
        return _response(run, _describe(run))
    found = _active_run()
    return found if isinstance(found, dict) else _response(found, _describe(found))


def _tick(payload: dict, tools: Tools) -> dict:
    found = _active_run()
    return found if isinstance(found, dict) else _advance(tools, found, names.ACTION_TICK)


def _latest_entry_into_running(run: state.Run) -> str | None:
    entries = [entry["at"] for entry in run.history if entry["to"] == names.STATE_RUNNING]
    return max(entries) if entries else None


def _alarm(payload: dict, tools: Tools) -> dict:
    found = _active_run()
    if isinstance(found, dict):
        return found
    run = found
    if run.state not in _ROLLBACK_SOURCES:
        return _response(run, f"{_describe(run)} The alarm has nothing to do in {run.state}.")
    latest = _latest_entry_into_running(run)
    if latest is not None and names.utc_iso(payload["at"]) < latest:
        return _response(
            run,
            f"{_describe(run)} The alarm at {payload['at']} is older than the run's latest "
            f"entry into {names.STATE_RUNNING}, {latest}; nothing to do.",
        )
    if run.pending:
        if run.pending == names.STATE_ROLLED_BACK and _stale(run):
            return _transit(tools, run, names.STATE_ROLLED_BACK, names.ACTION_ALARM)
        return _pending(run, engineer=False)
    return _transit(tools, run, names.STATE_ROLLED_BACK, names.ACTION_ALARM)


def _legal(action: str, source: str) -> bool:
    if action == names.ACTION_CHECK:
        return source in names.CHECK_LEGAL_STATES
    return any(t.source == source and action in t.triggers for t in names.TRANSITIONS)


def _engineer(payload: dict, tools: Tools) -> dict:
    """approve, decline and check: the pointer first, then legality, then the action."""
    action, run_id = payload["action"], payload["run_id"]
    pointer = state.get_active()
    if pointer is not None and pointer.run_id != run_id:
        return _bare(
            run_id,
            f"run {pointer.run_id} is active, not {run_id}; act on {pointer.run_id}",
            names.ERROR_RUN_ACTIVE,
        )
    try:
        run = state.load_run(run_id)
    except state.NotFound:
        return _bare(run_id, f"no run {run_id}", names.ERROR_NOT_FOUND)
    if pointer is None and run.state == names.STATE_PLANNED:
        if action != names.ACTION_DECLINE:
            return _response(
                run,
                f"run {run_id} has no pointer, so it was reset and can never launch: start a "
                "new one",
                names.ERROR_ILLEGAL_IN_STATE,
            )
        if run.pending == names.STATE_RUNNING:  # only a pending launch keeps it PLANNED
            return _pending(run, engineer=True, reset=True)
    if not _legal(action, run.state):
        return _response(
            run,
            f"{action} is not legal for run {run_id} in {run.state}",
            names.ERROR_ILLEGAL_IN_STATE,
        )
    if action == names.ACTION_CHECK:
        return _advance(tools, run, action)
    if action == names.ACTION_DECLINE:
        return _decline(tools, run)
    return _approve(tools, run)


def _decline(tools: Tools, run: state.Run) -> dict:
    target = names.STATE_ABORTED if run.state == names.STATE_PLANNED else names.STATE_HELD
    if run.pending:  # decline never takes over a transition another action claimed
        if run.pending == target and _stale(run):
            return _transit(tools, run, target, names.ACTION_DECLINE)
        return _pending(run, engineer=True)
    return _transit(tools, run, target, names.ACTION_DECLINE)


def _approve(tools: Tools, run: state.Run) -> dict:
    if run.pending:  # only a transition approve itself triggers may be taken over
        if names.ACTION_APPROVE in _row(run.state, run.pending).triggers and _stale(run):
            return _transit(tools, run, run.pending, names.ACTION_APPROVE)
        return _pending(run, engineer=True)
    if run.state == names.STATE_PLANNED:
        return _transit(tools, run, names.STATE_RUNNING, names.ACTION_APPROVE)
    if run.state == names.STATE_AWAITING_RESUME:
        outcome, why = _probe(tools)
        if outcome == _ALARM:
            return _response(run, f"{_describe(run)} Not now: {why}; approve once it is OK again.")
        if outcome == _PASSED:
            return _transit(tools, run, names.STATE_RUNNING, names.ACTION_APPROVE)
        return _transit(tools, run, names.STATE_ROLLED_BACK, names.ACTION_APPROVE, {"why": why})
    # RESULTS_READY: the alarm first, then the experiment's status.
    alarm = tools.alarm_state(names.ALARM_NAME)
    if alarm.state == names.ALARM_STATE_ALARM:
        return _transit(tools, run, names.STATE_ROLLED_BACK, names.ACTION_APPROVE)
    status = tools.flagsmith.experiment_status(run.item["experiment_id"])
    if not flagsmith.is_ended(status):
        return _response(
            run,
            f"experiment {run.item['experiment_id']} is {status!r}, not ended: end it in the "
            f"Flagsmith dashboard, then `marshal approve {run.run_id}` again",
            names.ERROR_EXPERIMENT_NOT_ENDED,
        )
    return _transit(tools, run, names.STATE_PROMOTED, names.ACTION_APPROVE)


_HANDLERS: dict[str, Callable[[dict, Tools], dict]] = {
    names.ACTION_START: _start,
    names.ACTION_STATUS: _status,
    names.ACTION_APPROVE: _engineer,
    names.ACTION_DECLINE: _engineer,
    names.ACTION_CHECK: _engineer,
    names.ACTION_ALARM: _alarm,
    names.ACTION_TICK: _tick,
}


def handle(payload: Any, tools: Tools) -> dict:
    """One action, answered with a response.json object; it never raises. A call that fails
    before any claim is UPSTREAM_FAILED, and nothing changed."""
    if not is_invoke_payload(payload):
        return _bare(
            None,
            f"not an invoke payload (contracts/schemas/invoke.json): {str(payload)[:_SHOWN]}",
            names.ERROR_BAD_REQUEST,
        )
    try:
        return _HANDLERS[payload["action"]](payload, tools)
    except Exception as error:  # noqa: BLE001 - the runtime always answers
        run_id = payload.get("run_id")
        return _bare(
            run_id,
            f"{payload['action']} failed before any claim, so nothing changed: {_shown(error)}",
            names.ERROR_UPSTREAM_FAILED,
        )
