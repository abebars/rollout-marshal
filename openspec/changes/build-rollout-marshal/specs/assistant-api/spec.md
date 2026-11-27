# Delta for assistant-api

## ADDED Requirements

### Requirement: Variant routing
The router SHALL serve each identity the model its Flagsmith experiment
variation assigns, unless the kill switch is off.

#### Scenario: Assigned cheaper, kill switch on
- **WHEN** an identity assigned `cheaper` asks a question and `assistant_cheaper_enabled` is enabled
- **THEN** `rm-answer-cheaper` answers and the exposure is recorded

#### Scenario: Kill switch off
- **WHEN** `assistant_cheaper_enabled` is disabled
- **THEN** every identity is served by `rm-answer-current`
- **AND** latency is recorded under `Variant=current`

#### Scenario: No active run
- **WHEN** there is no `ACTIVE` pointer, or it has no `feature_key`
- **THEN** the router serves the model named by `assistant_default_model`
- **AND** records no exposure or event, and does not invoke `rm-judge`

#### Scenario: Assigned comes from the flag's value
- **WHEN** an identity in the experiment is served
- **THEN** the judge payload's `assigned` is the experiment feature's value (`current` or `cheaper`)
- **AND** the exposure carries `flag.variant`

### Requirement: Request budget
The router MUST make exactly one Flagsmith identity request per answer.

#### Scenario: One answer
- **WHEN** one question is answered
- **THEN** the Flagsmith client received one identity request

### Requirement: Outcomes off the request path
Judging and Flagsmith events SHALL happen asynchronously and MUST NOT be
lost when the function freezes. The one exception is the router's exposure
flush: the router waits for it only while Lambda has time left (design.md
section 4), and a flush still running then is logged, losing at most that
request's exposure.

#### Scenario: Events flushed
- **WHEN** the judge handles one answer
- **THEN** `answer_helpful` (1 or 0), `answer_latency_ms` and `answer_cost_micro_usd` are sent
- **AND** `flush_events()` completes before the handler returns

#### Scenario: Judge reply unparseable
- **WHEN** the judge model returns something other than the expected JSON
- **THEN** `answer_helpful` is sent with value 0 and the raw reply is logged

### Requirement: Bounded load
The load generator MUST stop at its configured count and MUST NOT run two
loads at once.

#### Scenario: Stops at count
- **WHEN** `scripts/load.py --run-id <run_id> --users 150 --questions 10 --url <api>` finishes
- **THEN** exactly 1,500 requests were sent

#### Scenario: Second start
- **WHEN** a load run is started while another is in progress, from any worktree
- **THEN** the second is refused, because the first holds `rm-load.lock` in the git common directory
