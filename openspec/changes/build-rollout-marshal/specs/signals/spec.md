# Delta for signals

## ADDED Requirements

### Requirement: Alarm wakes the agent
A latency alarm on the cheaper variant SHALL invoke the agent with an
`alarm` action without anyone prompting it.

#### Scenario: Alarm enters ALARM
- **WHEN** `rm-cheaper-latency-p95` changes to ALARM
- **THEN** `rm-invoker` sends one `alarm` payload that validates against `contracts/schemas/invoke.json`

#### Scenario: Alarm leaves ALARM
- **WHEN** the alarm changes to OK or INSUFFICIENT_DATA
- **THEN** `rm-invoker` sends nothing

### Requirement: Tick while a run is active
A schedule SHALL invoke the agent with `tick` every 2 minutes while a run
is active and MUST be disabled otherwise.

#### Scenario: Run ends
- **WHEN** a run reaches `PROMOTED`, `HELD` or `ABORTED`
- **THEN** the `rm-tick` schedule is disabled

### Requirement: Manual fallback
The engineer SHALL be able to do by hand what the alarm or tick would do.

#### Scenario: Alarm path broken
- **WHEN** the engineer runs `marshal check <run_id>` on a `RUNNING` run while the alarm is in ALARM
- **THEN** the agent rolls back exactly as it would on an `alarm` action

### Requirement: Repeatable fault
The project SHALL provide a one-command fault that slows the cheaper
variant enough to fire the alarm and then clears by itself.

#### Scenario: Fault injected during load
- **WHEN** `scripts/inject_fault.sh` runs while a load run is in progress
- **THEN** the alarm reaches ALARM within 5 minutes
- **AND** the fault ends without manual cleanup
