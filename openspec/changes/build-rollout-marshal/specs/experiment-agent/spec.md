# Delta for experiment-agent

## ADDED Requirements

### Requirement: Plan from a prompt
The agent SHALL turn one engineer prompt into an experiment plan and SHALL
NOT change anything in Flagsmith until the engineer approves it.

#### Scenario: Plan awaits approval
- **WHEN** the engineer runs `marshal start` with a prompt naming the model move
- **THEN** a run is created in state `PLANNED` and becomes the active run
- **AND** the response shows the hypothesis, metrics, split and stop rules
- **AND** no Flagsmith feature or experiment exists for that run

#### Scenario: Plan declined
- **WHEN** the engineer runs `marshal decline` on a `PLANNED` run
- **THEN** the run moves to `ABORTED` and nothing is created in Flagsmith

### Requirement: Launch through Flagsmith
On approval the agent SHALL create the feature, multivariate options and
experiment through the Flagsmith MCP server, and SHALL create the metrics
and start the experiment through the Flagsmith Admin API.

#### Scenario: Launch
- **WHEN** the engineer approves a `PLANNED` run
- **THEN** feature `assistant_model_<run_id>` exists with value `current` and a keyed option `cheaper` at 50 percent
- **AND** the experiment is running in Flagsmith
- **AND** the run is `RUNNING` and is the active run

### Requirement: Automatic rollback
The agent SHALL roll back without human approval when the latency alarm
fires, and SHALL tell the engineer what it saw and did.

#### Scenario: Alarm during a run
- **WHEN** an `alarm` action arrives for a `RUNNING` run
- **THEN** `assistant_cheaper_enabled` is disabled
- **AND** the run is `ROLLED_BACK` with a new `incidents` entry whose `started_at` is set
- **AND** an incident note is published to `rm-incidents`

#### Scenario: Duplicate alarm
- **WHEN** a second `alarm` action arrives while the run is `ROLLED_BACK` or `AWAITING_RESUME`
- **THEN** no second rollback or note happens

#### Scenario: Late alarm after a resume
- **WHEN** an `alarm` action arrives whose `at` is earlier than the run's latest entry into `RUNNING`
- **THEN** nothing changes

#### Scenario: Second fault after a resume
- **WHEN** the alarm fires again for a `RUNNING` run that was resumed
- **THEN** the run is `ROLLED_BACK` with a second `incidents` entry
- **AND** a second incident note is published

#### Scenario: Alarm after results are ready
- **WHEN** the alarm is in ALARM for a `RESULTS_READY` run, through an `alarm`, `tick`, `check` or `approve`
- **THEN** `assistant_cheaper_enabled` is disabled and the run is `ROLLED_BACK` with a new `incidents` entry
- **AND** an `approve` at that moment rolls back instead of promoting

### Requirement: Resume needs a human
The agent SHALL NOT send traffic back to the cheaper model until a health
probe passes AND the engineer approves.

#### Scenario: Probe passes
- **WHEN** a `tick` arrives for a `ROLLED_BACK` run, the alarm is not in ALARM and the probe passes
- **THEN** the run is `AWAITING_RESUME` and a resume request is published

#### Scenario: Alarm still in ALARM
- **WHEN** a `tick` arrives for a `ROLLED_BACK` run while the alarm is still in ALARM
- **THEN** the probe does not run and the run stays `ROLLED_BACK` with no `pending`

#### Scenario: Probe fails
- **WHEN** a `tick` arrives for a `ROLLED_BACK` run and the probe fails
- **THEN** the run stays `ROLLED_BACK`

#### Scenario: Resume approved
- **WHEN** the engineer approves an `AWAITING_RESUME` run, the alarm is not in ALARM and the probe passes again
- **THEN** `assistant_cheaper_enabled` is enabled and the run is `RUNNING`
- **AND** the open `incidents` entry gets its `resumed_at`

### Requirement: Decision report
When results are ready the agent SHALL write a report with a recommendation
decided by fixed rules, and SHALL state any incident and the bias it causes.

#### Scenario: Results ready after an incident
- **WHEN** both variations have at least 50 identities and results are non-null
- **AND** the run had an incident
- **THEN** the report recommends `hold, re-run` and names the bias
- **AND** the run is `RESULTS_READY`

#### Scenario: Clean run that qualifies
- **WHEN** there was no incident, the lower bound of the 95 percent credible interval for the lift on `helpful_rate` is at or above -0.05, and both guardrails pass
- **THEN** the report recommends `promote`

### Requirement: Promotion needs a human
The agent SHALL NOT promote without the engineer ending the experiment in
Flagsmith and approving.

#### Scenario: Approve while still running
- **WHEN** the engineer approves a `RESULTS_READY` run whose experiment has not been ended, and the alarm is not in ALARM
- **THEN** the response says to end the experiment in Flagsmith first
- **AND** the run stays `RESULTS_READY`

#### Scenario: Approve after ending
- **WHEN** the experiment is ended and the engineer approves
- **THEN** `assistant_default_model` is `cheaper` and the run is `PROMOTED`

### Requirement: Safe under concurrency and bad input
The agent MUST apply each transition at most once and MUST refuse an action
that is not legal in the run's current state without side effects.

#### Scenario: Two callers race
- **WHEN** an `alarm` and a `tick` act on the same `RUNNING` run at once
- **THEN** exactly one transition is applied

#### Scenario: Second run while one is active
- **WHEN** `marshal start` is sent while a run is active (the pointer exists)
- **THEN** the response carries error `RUN_ACTIVE` and no run is created

#### Scenario: Engineer stops a run
- **WHEN** the engineer declines a `RUNNING` or `ROLLED_BACK` run
- **THEN** the run is `HELD`, the pointer is cleared and the tick is disabled
- **AND** the kill switch is left as it was

#### Scenario: Action on another run
- **WHEN** the engineer approves, declines or checks a run that the pointer does not name
- **THEN** the response carries error `RUN_ACTIVE` naming the active run and nothing changes

#### Scenario: Approve names what it approves
- **WHEN** the engineer approves with an `awaiting` the run does not await, such as `promotion` for a run an alarm has since moved to `AWAITING_RESUME`
- **THEN** the response carries error `ILLEGAL_IN_STATE` naming what the run awaits, and nothing changes

#### Scenario: A reset run cannot launch
- **WHEN** the engineer approves or checks a `PLANNED` run that has no pointer
- **THEN** the response carries error `ILLEGAL_IN_STATE` and nothing changes, while `decline` still ends it unless a launch is pending on it (such a run stays `PLANNED` and affects nothing)

#### Scenario: Restart within the same minute
- **WHEN** `start`, `decline` and `start` again happen within one UTC minute
- **THEN** the second `start` returns `BAD_REQUEST` ("start again next minute") and the declined run is unchanged

#### Scenario: Effect fails mid-transition
- **WHEN** a side effect throws after the transition was claimed
- **THEN** the run keeps its state with `pending` set
- **AND** the response carries error `EFFECT_FAILED` naming the effect and `marshal check <run_id>`
- **AND** a later `tick` or `marshal check` completes the same transition without repeating finished effects

#### Scenario: Decline while a transition is pending
- **WHEN** the engineer declines a run while another action's transition is still pending
- **THEN** the response carries error `TRANSITION_PENDING` and nothing changes

#### Scenario: Illegal action
- **WHEN** `approve` is sent for a `RUNNING` run
- **THEN** the response carries error `ILLEGAL_IN_STATE` naming the state and nothing changes
