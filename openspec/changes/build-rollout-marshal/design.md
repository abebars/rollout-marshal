# Design: Rollout Marshal

Source of truth for every name, value and interface the workstreams share.
If a task and this file disagree, this file wins. To change anything here,
open a contract-change pull request (see CLAUDE.md); never drift silently.

## 1. What it is

An agent that runs a Bedrock model migration experiment end to end. An
engineer gives it one prompt. It plans the test, sets it up in Flagsmith,
watches it on AWS, rolls back on trouble, and asks a human before resuming
or promoting.

Hackathon: Toggle Wars (Flagsmith, WeMakeDevs), Track 2 "AI Agents and
Feature Flags". Judges look for real time savings, a human approving
changes, and clear output.

## 2. Fixed environment

| Item | Value |
| --- | --- |
| AWS account | Sandbox-2, `818928662830` |
| Region | `us-east-1` for everything, including every Bedrock call |
| Resource prefix | `rm-` |
| Language | Python 3.13, `arm64` Lambdas |
| Agent framework | Strands Agents on Amazon Bedrock AgentCore Runtime, created with the AgentCore CLI |
| AgentCore CLI | npm package `@aws/agentcore`, command `agentcore`, version 1.3.0 or later within 1.x. Its project lives under `agent/`; it deploys the runtime through its own CDK stack and bootstraps CDK on the first deploy |
| Infra as code | Terraform in `infra/` for everything except the agent runtime, which the AgentCore CLI deploys, the Gateway pieces in section 7, which `agent/scripts/setup_gateway.py` creates, and SSM parameter `/rm/agent-runtime-arn`, which task 2.7 writes after each deploy. Every plan and apply from `infra/` runs `make build` first, which runs each function's `build.sh` where it has one; a module may package with `archive_file` instead (Gate 1) |
| Terraform state | S3 backend, bucket `rm-tfstate-818928662830`, key `rollout-marshal/terraform.tfstate`, created by task 1.0. Only the integration lead runs `terraform apply`, after each infra merge, and attaches the plan output to the pull request. Workstream agents run `terraform validate` and `terraform plan` only. The one exception is task 1.2, below. For a task whose Pass needs its own Terraform applied (2.7, 4.3), the lead may apply that branch merged locally with `dev` before it merges, attaches the plan, and re-applies `dev` if it does not merge. While a branch is applied unmerged, every later apply includes it; branches are applied together only from one local merge, and only if they touch different modules |
| Review access | Role `rm-review-readonly` (AWS managed ReadOnlyAccess, plus a deny on secret values and on the state bucket's objects), created by the lead and used by goldfish reviewers through the AWS CLI profile `rm-review`, always as `--profile rm-review`: exported environment credentials override `AWS_PROFILE`, but not `--profile` |
| Timebox | 90 minutes per task unless the task says otherwise. Past it, stop and tell the lead |
| Flagsmith | SaaS, organisation 41656, project "Toggle Wars" (ID `55601`), environment "Development" |
| Flagsmith plan limits | Free: 50,000 API requests a month, 1 project, 1 seat |
| Repo | `github.com/abebars/rollout-marshal`, public |

Task 1.2's exception to the apply rule: agent B applies and destroys
`infra/spike/`, a separate root whose local state is never committed. B runs
`aws sts get-caller-identity` first. Its provider pins `us-east-1` and allows only this
account, as the root does. Every resource name starts with `rm-spike-`. The
spike function gets the secret's name, never its value, and reads the value
at run time. The plan, apply and destroy output go in the task's evidence
file, and the destroy runs before the pull request is reviewed.

## 3. Bedrock models

| Role | Model ID | Notes |
| --- | --- | --- |
| Current arm (control) | `us.anthropic.claude-fable-5-1` | Needs account data retention mode `aws_review` in us-east-1. Send no `temperature`, `top_p`, `topP`, `top_k` or `topK`. Thinking is always on. |
| Cheaper arm (treatment) | `us.anthropic.claude-sonnet-5` | |
| Judge | `us.anthropic.claude-haiku-5-5` | Scores helpfulness so neither arm grades itself. |
| The agent's own reasoning | `us.anthropic.claude-sonnet-5` | Plan, incident note, report prose. |

All calls use the `bedrock-runtime` Converse API. Do not send sampling
parameters to any model.

These models think by default, and `maxTokens` caps thinking plus text, so
a small budget returns no text at all:

- Answers (both arms): `maxTokens` 2000, thinking effort `low` through
  `additionalModelRequestFields` (`output_config.effort`). The system
  prompt asks for an answer of at most 120 words.
- Judge: thinking disabled, `maxTokens` 200.
- Agent prose: `maxTokens` 4000, thinking effort `low` (the same request
  fields as the answers).
- A response with `stopReason` `max_tokens` is an error, never a result.

Task 1.2 proved the request fields: effort `low` is `output_config.effort`
on both answer models, and thinking is disabled with `thinking.type`
`disabled`, on the judge's model (Haiku 5.5) and on Sonnet 5, each inside
`additionalModelRequestFields`. Fable 5.1 cannot disable thinking; Bedrock
rejects the request with a 400. Lambda timeouts: answer functions 25 seconds, router 29
seconds, judge 30 seconds, `rm-invoker` 300 seconds, `rm-flagsmith-admin` 30
seconds.

## 4. Flagsmith contract

| Thing | Name or value |
| --- | --- |
| Run ID | `r` + UTC `YYYYMMDDHHMM`, e.g. `r202611271530`. Scratch runs made by spikes and tests use `r` + a word + UTC `HHMM`, e.g. `rsmoke0412`. Pattern for both: `^r[a-z]*[0-9]{4,12}$` |
| Experiment feature (multivariate) | `assistant_model_<run_id>` |
| Control | the feature's own value, `current`. Flagsmith reserves the control; it is not a keyed option. Remote evaluation, exposures and results label it `control` (task 1.2), so `flag.variant` reads `control` while `flag.value` reads `current` |
| Treatment | one multivariate option with key `cheaper` and value `cheaper`, at 50 percent. The experiment's rollout block holds that 50 percent, and it decides the split whatever the option's own default allocation (task 1.4: 807's option defaulted to 0 and split 78 to 72). An option without a key records no exposures |
| Experiment rollout | 100 percent of identities, set when the experiment is created; a wrong one is fixed by hand on the experiment page |
| Kill-switch feature (boolean) | `assistant_cheaper_enabled`, default enabled |
| Events, one of each per answer | `answer_helpful` (value 1 or 0, integer), `answer_latency_ms` (value), `answer_cost_micro_usd` (value) |
| Metrics (environment level, get-or-create) | `helpful_rate` = mean of `answer_helpful` (primary, higher is better); `mean_latency_ms` = mean of `answer_latency_ms` (lower is better); `mean_cost_micro_usd` = mean of `answer_cost_micro_usd` (lower is better) |
| Expected direction per experiment metric | `helpful_rate`: should not decrease; `mean_latency_ms`: should not increase; `mean_cost_micro_usd`: should not increase |
| API spellings (task 1.3) | higher is better: `up`; lower is better: `down`; should not decrease: `not_decrease`; should not increase: `not_increase` |
| Identity format | the run ID, a hyphen, then `user-001` to `user-150` (`run_identity()` in names.py), so each run's identities are new to Flagsmith: nothing shows that an experiment's results count only events sent after its own exposures (Gate 1). The probe's identity reaches no Flagsmith call |
| SDK | `flagsmith` Python package 6.6.0 or later, remote evaluation, `enable_events=True` |
| Flags call | `get_identity_flags`, once per answer |
| Exposure call | `track_exposure_event(feature, identifier, value=flag.variant, metadata={"experiment_id": flag.experiment.id})`, only when the flag is enabled, `flag.experiment.in_experiment` and `flag.variant` is set |
| Outcome call | `track_event`, with the identity and a numeric `value`. The SDK sends the value as a string, and Flagsmith aggregates it as a number (task 1.2) |
| Flush | `flush_events()` before every handler returns. Buffer settings alone do not survive a Lambda freeze, and a buffer of one item drops events tracked in a row (task 1.2) |

Available metric aggregations are occurrence, count, sum and mean. There
is no "rate"; occurrence counts an identity once, which is useless at 10
answers per identity, so helpfulness is a mean of 1s and 0s.

The SDK rows above come from a read of the SDK source and docs during
review and are proven by task 1.2 before anything depends on them.

Rules:

- One experiment per run. Flagsmith allows one active experiment per
  feature and an ended experiment cannot restart, so each run gets a new
  feature key.
- The agent creates features, multivariate options and the experiment
  through the Flagsmith MCP server (`https://mcp.flagsmith.com`, header
  `Authorization: Api-Key <key>`, with exactly one space; `api_key_authorization()`
  in names.py builds it). The experiment's create carries its whole
  rollout block (rollout, the control's value, the option's allocation);
  Flagsmith refuses it without one (task 1.3). The API has no
  primary-metric field, and whether the dashboard takes the first attached
  metric as primary is untested, so the agent attaches `helpful_rate` first
  and task 5.1 records which metric the dashboard shows as primary.
- The MCP server cannot create a metric (its create_metric tool drops the
  required definition, task 1.3), start an experiment or refresh results.
  Those go through the Flagsmith Admin API from a small Lambda tool.
- Development and Production use v2 feature versioning: a feature's
  environment state changes through a new version (create it, update its
  default state, publish it), never in place (task 1.3).
- Ending an experiment is done by the engineer in the Flagsmith dashboard.
  The dashboard locks the flag until then, but the API accepted all three
  writes task 1.3 tried on a running experiment's feature, through MCP. So no agent changes
  an experiment's feature while it runs, and no agent ends an experiment or
  calls an end-experiment endpoint, or sends an experiment's status or
  rollout decision through MCP's experiment create or update tools, whose
  schemas list both (task 1.3). Task 2.2 enforces it before any call: an MCP
  tool outside its allow-list, an experiment create or update carrying
  `status`, `rollout_decision`, `rollout_variant`, `started_at` or
  `ended_at`, and any Admin API method and path but the GET and POST pairs
  it needs are refused: never DELETE, PUT or PATCH, and `complete/`,
  `pause/` and `rollout/` can never be built. The MCP allow-list leaves out
  `flagsmith___update_experiment` and every `flagsmith___delete_*` tool.
- One Flagsmith identity request per answer. Flag, identity, trait and
  environment-document requests count toward the 50,000 cap.
- Get-or-create checks what it finds. A found metric must match its
  definition on event, aggregation and direction. A found permanent feature
  is checked on type only: a standard feature, not multivariate, and the
  default-model feature holds a string value, `current` or `cheaper`. Its
  current state and value are never compared with the defaults, because
  rollback and promote change them; the defaults apply only on create. A mismatch stops with an error for the human to fix
  in the dashboard. Nothing updates itself.
- In spikes (tasks 1.2 and 1.3), every name created in Flagsmith starts
  with `spike_`: features, metrics and events. The one exception is task
  1.3's experiment feature, a new `assistant_model_rspike<HHMM>` on every
  attempt.

Two permanent features exist besides the per-run one. Both are created
get-or-create by `scripts/bootstrap_flagsmith.py` (task 3.5) and again,
idempotently, by the agent at launch:

- `assistant_cheaper_enabled` (boolean kill switch, default enabled).
- `assistant_default_model` (string value, `current` or `cheaper`, default
  `current`). This is what "promote" changes. It is never part of an
  experiment, so it is never locked.

Router evaluation order, per request:

1. Read the active run pointer (section 6). No active run: read
   `assistant_default_model` from the environment flags (cached 30
   seconds), serve that model, record no exposure and no event, and do not
   invoke `rm-judge`, so no `AnswerCostMicroUsd` or `AnswerHelpful` is
   emitted.
2. Call `get_identity_flags` once. Both the experiment feature and the
   kill switch are read from that one result. Do not call
   `get_experiment_flag`; it makes its own identity request.
3. If the experiment feature reports the identity is in the experiment,
   the flag is enabled and `flag.variant` is set, record the exposure with
   `track_exposure_event`, as the SDK's own `get_experiment_flag` would
   (task 1.2's finding 7).
4. If `assistant_cheaper_enabled` is disabled, serve `current` whatever the
   variation says, and set `served=current`. `assigned` is always the
   experiment feature's value for the identity (`current` or `cheaper`),
   not `flag.variant`; the exposure call sends `flag.variant`.
5. Otherwise serve the assigned variation.
6. `flush_events()` before returning.

Results can be refreshed at most once every 5 minutes. The run item keeps
`last_refresh_at`; a tick inside that window reads the last results
without refreshing. Results lag the events: in task 1.2 all events were in
by about 2 minutes 15 seconds after the last was sent, the exact lag
unmeasured, so a refresh right after a load can miss the last events.
After the engineer ends an experiment, a refresh returns final results,
as of the end (task 1.4).

## 5. Request path and metrics

```
scripts/load.py -> API Gateway HTTP API -> rm-router
rm-router -> (sync invoke) rm-answer-current | rm-answer-cheaper -> Bedrock
rm-router -> (async invoke) rm-judge -> Bedrock (judge) -> Flagsmith events
rm-router -> CloudWatch embedded metrics
```

- `rm-router` measures latency around the synchronous answer invoke, so an
  injected delay is visible to it.
- `rm-router` hands `rm-judge` this payload: `{run_id, identity, assigned,
  served, model_id, question, answer, input_tokens, output_tokens,
  latency_ms}`.
- Judging and Flagsmith events happen in `rm-judge`, off the request path.
  API Gateway's 30 second limit applies to the request path only.
- Answer Lambdas return whole responses. No response streaming (FIS Lambda
  faults do not support it).
- The answer functions take `question` and `identity` and return `answer`,
  `input_tokens`, `output_tokens` and `model_id` (schemas `answer_request`
  and `answer_response`).

CloudWatch namespace `RolloutMarshal`, dimension `Variant` = `current` or
`cheaper` (the variant actually served):

| Metric | Unit | Emitted by |
| --- | --- | --- |
| `AnswerLatencyMs` | Milliseconds | `rm-router` |
| `AnswerCostMicroUsd` | Count | `rm-judge` |
| `AnswerHelpful` | Count (0 or 1) | `rm-judge` |

Cost per answer = input tokens x input price + output tokens x output
price, in millionths of a US dollar, from `app/prices.json`. Prices are
copied from the AWS Bedrock pricing page at build time with the page URL
and date in the file. Do not invent prices.

Alarm `rm-cheaper-latency-p95`: `AnswerLatencyMs`, `Variant=cheaper`,
statistic p95, period 60 seconds, 2 of 2 datapoints above the threshold,
missing data treated as not breaching. Threshold default 10000 ms; task
4.1 recalibrates it to the baseline p95 that task 3.6 measures for
`Variant=cheaper`, plus half the injected delay (5000 ms), rounded up to
the next 1000, and records the number here. If that p95 is above 8000 ms,
it stops and asks the human first. The threshold reaches Terraform as the
signals module's variable `alarm_threshold_ms`, which the root fills from
`contracts/names.json`, so a recalibration is a contract change plus the
lead's re-apply.

## 6. State

DynamoDB table `rm-runs`, partition key `pk` (string).

Pointer item: `pk = "ACTIVE"`, attributes `run_id` and, once launched,
`feature_key`. "Active run" means the pointer exists. The pointer changes
only inside the same `TransactWriteItems` as the run-item write it mirrors:

- Create, for `start`: put the pointer on condition
  `attribute_not_exists(pk)`, returning the old item if that fails, and put
  the run item on the same condition. A failed pointer condition means
  `RUN_ACTIVE`, naming the run the old item names. A failed run-item
  condition alone means the run ID is in use: `start` returns
  `BAD_REQUEST`, "start again next minute", and writes nothing.
- Launch commit: the commit into `RUNNING`, plus an update that sets the
  pointer's `feature_key` on condition its `run_id` is this run.
- Terminal commit (`PROMOTED`, `HELD`, `ABORTED`): the commit, plus
  `clear_active`, a delete of the pointer on condition it is absent or
  names this run.

A transaction cancelled by a colliding write is a lost race: re-read, and
retry a commit once if the claim is still this invocation's. The router
treats a pointer without `feature_key` as "no active run". The router
caches the pointer for 30 seconds, so for up to 30 seconds after a terminal
state it may still serve the old run; that is accepted. A pointer whose run
item is missing also counts as no active run for `status`, `tick` and
`alarm`: a no-op whose message starts "no active run" and names the
pointer's run and `scripts/reset.py`. `start` still refuses it with
`RUN_ACTIVE`, and `approve`, `decline` and `check` on that run return
`NOT_FOUND`.

Run item: `pk = "RUN#<run_id>"` with:

| Attribute | Type | Meaning |
| --- | --- | --- |
| `run_id` | S | |
| `state` | S | One of the states below |
| `prompt` | S | The engineer's request |
| `plan` | M | The plan below (schema `contracts/schemas/plan.json`) |
| `feature_key` | S | `assistant_model_<run_id>` |
| `experiment_id` | N | Flagsmith experiment ID |
| `metric_ids` | M | name to Flagsmith metric ID |
| `incidents` | L | One entry per rollback: `started_at`, `resumed_at`; absent until the first rollback |
| `notified` | M | Notification dedupe key to the SNS message ID; written empty at create |
| `report_key` | S | S3 key of the report |
| `history` | L | Appended on every transition: `at`, `from`, `to`, `actor`, `reason` |
| `pending`, `pending_at` | S | Target state of a claimed, unfinished transition, and when it was claimed |
| `last_refresh_at` | S | When results were last refreshed |
| `first_running_at` | S | When the run first entered `RUNNING` |
| `created_at`, `updated_at` | S | ISO 8601 UTC |

A notification's dedupe key is `<history length when claimed>#<to state>`,
for example `2#ROLLED_BACK`, so a second rollback after a resume gets a key
of its own. A notification is published unless its key is already in
`notified`, then the key is recorded with the SNS message ID: at least
once, never lost. Neither a dedupe key nor an incident time comes from
`pending_at`, which a takeover rewrites.

The plan. The model writes only `hypothesis` and `summary`; every other
field is injected from names.py, so a plan whose values came from the
prompt fails validation:

| Field | Value |
| --- | --- |
| `hypothesis`, `summary` | written by the model |
| `current_model` | `us.anthropic.claude-fable-5-1` |
| `cheaper_model` | `us.anthropic.claude-sonnet-5` |
| `primary_metric` | `helpful_rate` |
| `guardrail_metrics` | `mean_latency_ms`, `mean_cost_micro_usd` |
| `split` | `current` 50, `cheaper` 50 |
| `min_identities_per_variation` | 50 |
| `stop_rules` | `rollback_alarm` `rm-cheaper-latency-p95`; `results_embargo_seconds` 600; `promote_requires_experiment_ended` true |
| `guardrails` | `max_latency_ratio` 1.1; `max_cost_ratio` 1.0 |
| `recommendation_rule` | `after_incident` `hold, re-run`; `promote_min_helpful_lift_ci_lower` -0.05 |

`start` returns `BAD_REQUEST` before any write when the prompt names no
model move, or any move other than Fable 5.1 to Sonnet 5.

States and the only legal transitions:

| From | To | Trigger | Actor |
| --- | --- | --- | --- |
| (none) | `PLANNED` | `start` | engineer |
| `PLANNED` | `RUNNING` | `approve` | engineer |
| `PLANNED` | `ABORTED` | `decline` | engineer |
| `RUNNING` | `ROLLED_BACK` | `alarm`; or `tick` or `check` when the alarm is in ALARM | agent |
| `RUNNING` | `RESULTS_READY` | `tick` or `check`, when the alarm is not in ALARM and results are ready | agent |
| `RUNNING` | `HELD` | `decline` | engineer |
| `ROLLED_BACK` | `AWAITING_RESUME` | `tick` or `check`, when the alarm is not in ALARM and the health probe passes | agent |
| `ROLLED_BACK` | `HELD` | `decline` | engineer |
| `AWAITING_RESUME` | `RUNNING` | `approve`, when the alarm is not in ALARM and the health probe passes again | engineer |
| `AWAITING_RESUME` | `ROLLED_BACK` | `approve`, when the health probe fails | agent |
| `AWAITING_RESUME` | `HELD` | `decline` | engineer |
| `RESULTS_READY` | `ROLLED_BACK` | `alarm`; or `tick`, `check` or `approve` when the alarm is in ALARM | agent |
| `RESULTS_READY` | `PROMOTED` | `approve`, when the alarm is not in ALARM, only if the experiment is ended in Flagsmith | engineer |
| `RESULTS_READY` | `HELD` | `decline` | engineer |

Terminal states: `PROMOTED`, `HELD`, `ABORTED`.

Rules that hold everywhere:

- **One run at a time.** `start` is refused while the pointer exists,
  that is, while a run is active. The pointer is written at `PLANNED` with the
  condition `attribute_not_exists(pk)`, so a second `start` fails on it.
  The terminal commit deletes the pointer in the same transaction
  (`clear_active`), on condition it is absent or names this run.
- **An engineer action on a run the pointer does not name returns
  `RUN_ACTIVE`** and names the active run. With no pointer at all,
  `approve`, `decline` and `check` go ahead, so a run left without a
  pointer can still be ended, with one exception: a `PLANNED` run with no
  pointer can never launch, so `approve` and `check` on it return
  `ILLEGAL_IN_STATE` (the run was reset; start a new one), while `decline`
  still ends it, unless a launch is pending on it; such a run stays
  `PLANNED` and, with no pointer, affects nothing. `scripts/reset.py`
  refuses while the pointer names a run
  in a non-terminal state, unless it is given `--force`.
- **`tick` and `check` in `RUNNING` or `RESULTS_READY` look at the alarm
  first**, then, in `RUNNING`, at results.
- **`check` is legal in every non-terminal state** (except on a `PLANNED`
  run with no pointer, above), where it does what a
  `tick` would do for that run, including finishing a stalled transition.
  In a terminal state it returns `ILLEGAL_IN_STATE`.
- **Results are not read in the first 600 seconds after
  `first_running_at`.** This keeps a fast-filling experiment from reaching
  `RESULTS_READY` before a fault can be shown.
- **Results are only read in `RUNNING`.** A tick in `ROLLED_BACK` or
  `AWAITING_RESUME` never moves a run to `RESULTS_READY`.
- **`alarm` or `tick` with no active run, or in a state where it has
  nothing to do, is a successful no-op**: response `message` says why, no
  error, no side effect. Only `start`, `approve`, `decline` and `check`
  return an error when not legal in the current state.
- **An alarm in `AWAITING_RESUME` or `ROLLED_BACK` is a no-op.** The kill
  switch is already off.
- **An alarm whose `at` is earlier than the run's latest entry into
  `RUNNING` is a no-op**, so a late or repeated delivery cannot roll back a
  resumed run.
- **The tick schedule is enabled first and disabled last**: launch enables
  it as its first effect and every terminal transition disables it as its
  last, so a tick can finish a stalled transition until only the commit is
  left.

How a transition is applied (guard, claim, effect, commit):

0. Guard: checks that decide WHETHER to move the run, made before any
   claim, that change nothing if they fail: the alarm state, the health
   probe, "results are ready", and "the experiment is ended in Flagsmith".
   A failed guard returns the current status (or `EXPERIMENT_NOT_ENDED`)
   and leaves no `pending`. The one exception is `approve` in
   `AWAITING_RESUME` with a failing probe, which is itself a transition to
   `ROLLED_BACK`. A Bedrock, AWS or Flagsmith call that fails before any
   claim returns `UPSTREAM_FAILED`. The health probe is the exception: an
   invoke error, a timeout, an invalid reply or a failed alarm read fails
   the probe.

1. Claim: conditional write that sets `pending = <to>` and
   `pending_at = <now>`, on condition `state = <from>` and `pending` is
   absent. If the condition fails, another invocation owns it: re-read and
   return the current status, with `TRANSITION_PENDING` for an engineer
   action while the transition is still pending. Do nothing else.
   Takeover: a `pending` older than 360 seconds may be taken over by one
   conditional write that sets `pending_at = <now>` on condition
   `state = <from>`, `pending = <to>` and `pending_at` still holding the
   value read. Only the winner runs the effects. `tick` and `check` may
   take over any transition; any other action only one its row lists as a
   trigger. `decline` never takes over a transition another action
   claimed.
2. Effects: run the side effects below. Every effect is idempotent
   (get-or-create by key, set-to-value, publish unless its key is already
   in `notified`).
3. Commit: write `state = <to>`, remove `pending`, append to `history`, on
   condition `state = <from>`, `pending = <to>` and `pending_at` holding
   the value this invocation wrote. If the condition fails, re-read and
   return the current status, with `TRANSITION_PENDING` for an engineer
   action while the transition is still pending.

If an effect throws, the invocation releases its own claim, moving
`pending_at` back by 360 seconds on condition the claim is still its own,
and returns `EFFECT_FAILED`. Its message names the failed effect, the
command `marshal check <run_id>`, and when that will act. Any later
`tick`, `check` or repeat of the same action that finds `pending` older
than 360 seconds resumes from step 2 for that same target state; only an
invocation that died leaves a claim to wait out. A resumed
`RUNNING -> RESULTS_READY` reads the last results again and writes the
report prose inside the effect.

What each transition does (the pointer changes are in the commits above):

- `(none) -> PLANNED`: the create transaction: the run item, with
  `notified` empty, and the pointer.
- `PLANNED -> RUNNING` (launch): enable the tick schedule; get-or-create
  the two permanent features; set `assistant_cheaper_enabled` enabled;
  get-or-create the run's feature, options and experiment through the MCP
  server, and its metrics through the Admin API tool; start the experiment
  through the Admin API tool.
- `RUNNING -> ROLLED_BACK` and `RESULTS_READY -> ROLLED_BACK`: disable
  `assistant_cheaper_enabled`; publish the incident note to SNS. The
  commit appends an `incidents` entry with `started_at`.
- `ROLLED_BACK -> AWAITING_RESUME` (guard: the alarm is not in ALARM, then
  the health probe passes): publish a resume request to SNS. The probe is 5
  direct invokes of `rm-answer-cheaper`, run in parallel, each asking
  `How do I reset my password?` as `rprobe0000-user-001`. It passes if the slowest is
  under the alarm's threshold, read from the same DescribeAlarms call as
  the alarm's state; a failed read, an invoke error, a timeout or a reply
  that fails `answer_response` fails it.
- `AWAITING_RESUME -> RUNNING` (guard: the alarm is not in ALARM, then the
  probe passes again; an ALARM returns the status with a message): enable
  `assistant_cheaper_enabled`. The commit sets `resumed_at` on the open
  `incidents` entry.
- `AWAITING_RESUME -> ROLLED_BACK` (the probe failed on `approve`):
  publish a note saying the cheaper model is slow again. It adds no
  `incidents` entry.
- `RUNNING -> RESULTS_READY` (guard, all before the claim: the alarm is not
  in ALARM, results refreshed through the Admin API tool are `ready`, and
  the report prose is written): write the report to S3 with a plain put,
  so S3 keeps the report of the latest entry into `RESULTS_READY`; publish
  it to SNS.
- `RESULTS_READY -> PROMOTED` (guard, in this order: the alarm is read
  first, and an alarm in ALARM rolls the run back instead; then the
  experiment is ended): set
  `assistant_default_model` to `cheaper`; disable the tick schedule. From
  then on the router serves `cheaper` to everyone.
- Any transition into `HELD`: disable the tick schedule; leave everything
  in Flagsmith as it is, including the kill switch.
  `assistant_default_model` is not changed, so the router keeps serving
  `current`.
- `PLANNED -> ABORTED`: disable the tick schedule (normally a no-op).
  Nothing in Flagsmith is touched, because nothing was created.

Guardrails pass when the cheaper arm's `mean_latency_ms` is at most 110
percent of the current arm's, and its `mean_cost_micro_usd` is not higher
than the current arm's.

Recommendation rule (in code, not in the prompt):

- An incident happened during the run (`incidents` is not empty): recommend
  `hold, re-run`, and state that rolled-back users still count in the
  cheaper arm.
- Otherwise recommend `promote` only if the lower bound of the 95 percent
  credible interval that Flagsmith gives for the lift on `helpful_rate` is
  at or above minus 5 percent (-0.05, a fraction of relative lift) and both
  guardrails pass. That interval is the lift plus or minus 1.96 standard
  deviations (task 1.4's sample), so the rule asks for at least a 97.5
  percent probability that the lift is at least -0.05; the report states
  it that way.
- Otherwise recommend `hold`.

Flagsmith's results carry no "not worse" probability (task 1.4), so the
rule uses the interval alone. Task 2.2's results mapping is finished against the
response task 3.6 records. Every planned end-to-end run injects the fault,
so each one recommends `hold, re-run`: the promote branch is covered by
unit tests, and no report, README or video says a run recommended
`promote` unless one did.

Ended in Flagsmith means the experiment's status is `completed`; `paused`
is not ended.

## 7. Invoking the agent

One AgentCore Runtime agent, `rm_agent`, in the AgentCore CLI project `rm`
with target `default`. The CLI names the deployed runtime
`<project>_<target>_<runtime>`, so it is `rm_default_rm_agent`, and
callers use its ARN (task 1.3). Each invocation is short and
stateless and uses a new session ID (a UUID4 with the prefix `rm-session-`,
which is longer than the 33 character minimum). Payload schema
`contracts/schemas/invoke.json`:

```json
{"action": "start", "prompt": "<text>", "actor": "engineer"}
{"action": "approve", "run_id": "<id>", "actor": "engineer"}
{"action": "decline", "run_id": "<id>", "actor": "engineer"}
{"action": "status", "run_id": "<id or omitted for the active run>"}
{"action": "check", "run_id": "<id>", "actor": "engineer"}
{"action": "alarm", "alarm_name": "rm-cheaper-latency-p95", "alarm_state": "ALARM", "at": "<ISO 8601>"}
{"action": "tick"}
```

Response (`contracts/schemas/response.json`):
`{"run_id": str | null, "state": str | null, "message": str,
"awaiting": null | "launch" | "resume" | "promotion",
"pending": str | null,
"error": null | {"code": str, "message": str}}`.
`pending` is the target of a claimed, unfinished transition, or null.
Error codes: `ILLEGAL_IN_STATE`, `RUN_ACTIVE`, `NOT_FOUND`,
`EXPERIMENT_NOT_ENDED`, `EFFECT_FAILED`, `BAD_REQUEST`, `TRANSITION_PENDING`,
`UPSTREAM_FAILED`.

Legality is judged after the `RUN_ACTIVE` check and before anything
pending: an action the table does not allow from the run's state returns
`ILLEGAL_IN_STATE` whatever is pending.

- `TRANSITION_PENDING`: an engineer action lost its claim or commit to
  another invocation, or another transition is pending that it may not
  take over. The message names the pending target, its age and
  `marshal check <run_id>` (for a `PLANNED` run with no pointer: the run
  was reset; start a new one).
- `UPSTREAM_FAILED`: a Bedrock, AWS or Flagsmith call failed before any
  claim, so nothing changed. After a claim, every effect failure, and a
  commit that fails other than by a lost race, is `EFFECT_FAILED`.

`status` with no active run returns `run_id`, `state`, `awaiting`,
`pending` and `error` all null, and a message that starts "no active run".

Results shape used by the tools (`contracts/schemas/results.json`):
`{"ready": bool, "variations": {"current": V, "cheaper": V},
"helpful_lift_ci_lower": float | null}` where
`V = {"identities": int, "helpful_rate": float | null,
"mean_latency_ms": float | null, "mean_cost_micro_usd": float | null}`.
The results tool computes `ready`: both variations have at least 50
identities and non-null `helpful_rate`, `mean_latency_ms` and
`mean_cost_micro_usd`. `helpful_lift_ci_lower` is the lower bound of
Flagsmith's 95 percent credible interval for the relative lift
(cheaper - current) / current on `helpful_rate`, as a fraction (-0.05 is
minus 5 percent); it can fall below -1, because the interval is the lift
plus or minus 1.96 standard deviations. Flagsmith's
results give, per metric, each variant's `n`, `sum` and `sum_squares` (the
mean is sum / n) and, for `cheaper`, its `lift` (relative, a fraction),
`ci_low`, `ci_high` and `chance_to_win`; the control arm is `control`
(task 1.4's sample). So `helpful_lift_ci_lower` is the `helpful_rate`
metric's `ci_low`. Flagsmith has no "not worse" probability, so the
shape carries none. A variation's `identities` is the sum of its
`new_identities` in the same response's exposures timeseries, not `n`:
in the sample each identity sent one event, so whether `n` counts
identities or events stays open until task 3.6. Results are read through
the Admin API tool; MCP's results read returned nothing before a refresh
(task 1.3).
Task 2.2 maps Flagsmith's result fields onto this shape, starting from the
sample task 1.4 records and finished against the response recorded in task
3.6. Schemas in `contracts/schemas/`: `invoke`, `response`, `plan`,
`run_item`, `results`, `judge_payload`, `answer_request`, `answer_response`.

Three things invoke it:

| Caller | Sends | How |
| --- | --- | --- |
| `marshal` CLI (`cli/`) | `start`, `approve`, `decline`, `status`, `check` | `InvokeAgentRuntime` with the engineer's AWS credentials, through the console script `cli.marshal:main` |
| `rm-invoker` Lambda | `alarm` | EventBridge rule `rm-alarm-to-invoker` on the alarm entering ALARM. Role `rm-invoker-role`. Reads the runtime ARN from SSM parameter `/rm/agent-runtime-arn`. Terraform variable `invoker_stub` (root, set by the lead) sets env `STUB`; with `STUB=1` it only logs the payload and never reads the parameter |
| `rm-tick` schedule | `tick` | EventBridge Scheduler, every 2 minutes, enabled by launch and disabled when the run reaches a terminal state; targets `rm-invoker`, with the tick payload as its input. It sits in the `default` schedule group, with execution role `rm-scheduler-role`, which may invoke only `rm-invoker`. An update replaces every field of a schedule, so the agent's `schedule` tool reads it and writes back the same target, input and role, changing only its state |

The CLI reads the runtime's ARN from `/rm/agent-runtime-arn` with the
engineer's credentials, prints the response, and exits 0 when its error is
null, 1 when it carries an error code, and 2 for a usage error. The root
project is packaged from task 2.6 on, with a hatchling build of `cli` and
`contracts`, so the console script installs and `uv run marshal` works;
the lead switches it on when 2.6 merges (Gate 1: unpackaged, uv installs no
console script).

The CLI and `rm-invoker` share one `InvokeAgentRuntime` client setting: read
timeout 290 seconds and one attempt (`total_max_attempts` 1). It sits just
under the invoker's own timeout, so the client normally gives up before
Lambda stops it, and Lambda's retry of the asynchronous event is the only
retry.

`check` is the manual fallback for the alarm path: it does whatever `alarm`
or `tick` would do for the run's current state.

The model is used for three things only: turning the prompt into a plan,
writing the incident note, and writing the report prose. State changes,
stop rules and the recommendation are deterministic code.

Agent tools:

| Tool | Reaches | Through |
| --- | --- | --- |
| Flagsmith MCP tools | features, multivariate options, experiments | AgentCore Gateway MCP target, API key from AgentCore Identity |
| `flagsmith_admin` | get-or-create metrics, start experiment, refresh and read results, read experiment status | Lambda `rm-flagsmith-admin` as a Gateway target |
| `state` | `rm-runs` | boto3 in the agent |
| `signals` | alarm state, CloudWatch metric statistics | boto3 in the agent |
| `probe` | 5 direct invokes of `rm-answer-cheaper` | boto3 in the agent |
| `schedule` | enable or disable the `rm-tick` schedule | boto3 in the agent |
| `notify` | SNS topic `rm-incidents` | boto3 in the agent |
| `report` | S3 `rm-reports-818928662830` | boto3 in the agent |

AgentCore pieces. `agent/scripts/setup_gateway.py` gets or creates each one
by name, so it is safe to repeat; the CLI's gateway commands may be used
instead under the same names, except for two pieces (task 1.3): the
credential provider, because the CLI either stores the key in its local env
file or takes it from a JSON-keyed secret, and `rm/flagsmith/api-key` holds
a bare string; and the `flagsmith` target, which CLI 1.3.0 cannot create
with API-key auth:

| Piece | Name | Notes |
| --- | --- | --- |
| Runtime | `rm_agent` | One AgentCore CLI project under `agent/`, deployed from task 1.3 on. The CLI packages only its code folder, which becomes the zip root, so the code lives in `agent/runtime/` (the CLI's `codeLocation` `runtime`, entrypoint `app.py`) as the package `rm_agent`; tests in `agent/tests/` import it through the root pytest `pythonpath`, and task 2.7 gives the folder its own `pyproject.toml`, `uv.lock` and a `contracts` symlink. Task 2.7 redeploys the same runtime, so its ARN and `/rm/agent-runtime-arn` never change |
| Gateway | `rm-gateway` | Protocol `MCP`, inbound auth `AWS_IAM`, so no Cognito pool. The agent signs with SigV4 through `mcp-proxy-for-aws`, and the runtime's role gets `bedrock-agentcore:InvokeGateway` on the Gateway |
| Gateway service role | `rm-gateway-role` | Also the outbound auth of the Lambda target, which supports only the service role |
| Credential provider | `rm-flagsmith-api-key` | Holds the organisation key from `rm/flagsmith/api-key`, passed in the `apiKey` field of `CreateApiKeyCredentialProvider`, never through the CLI's env file |
| MCP server target | `flagsmith` | `https://mcp.flagsmith.com`, API-key outbound auth, header `Authorization`, prefix `Api-Key`; the service stores the prefix trimmed (task 1.3) |
| Lambda target | `flagsmith-admin` | Lambda `rm-flagsmith-admin`, added once task 2.2 has deployed it; `agent/scripts/setup_gateway.py` is finished in task 2.7 |

The runtime's role is the CLI stack's own: `AgentCore-rm-default` creates
it with the Bedrock, logging and Gateway grants. Task 2.7's Terraform reads
its ARN from the stack's export `AgentCore-rm-default-rm-agent-RoleArn` and
attaches
inline policy `rm-agent-runtime-tools`: read and write on `rm-runs`,
publish to `rm-incidents`, get and put on the reports bucket, read alarms
and metrics, invoke `rm-answer-cheaper` for the probe, and get and update
`rm-tick` with pass-role on `rm-scheduler-role`.

A target's name shapes the tool names the Gateway gives its tools, so these
names never change: the Flagsmith tools appear as `flagsmith___<tool>`, all
54 listed in task 1.3's evidence. Stack and role names that the CLI generates without the
`rm-` prefix are accepted, except the Gateway service role: it is always
`rm-gateway-role`, passed to the CLI when its gateway commands are used.

Fallback if Gateway auth to the Flagsmith MCP server is not working after
90 minutes of task 1.3: connect the agent directly to `https://mcp.flagsmith.com`
with the API key header and record the decision in `evidence/`.

## 8. Secrets and notifications

| Name | Holds | Read by |
| --- | --- | --- |
| Secrets Manager `rm/flagsmith/api-key` | Flagsmith organisation API key (MCP and Admin API) | Gateway credential provider, `rm-flagsmith-admin`, and every local script or test that calls Flagsmith's MCP server or Admin API directly: task 1.3's probes, `agent/scripts/setup_gateway.py`, live tests, `scripts/bootstrap_flagsmith.py`, `scripts/seed_experiment.py`, `scripts/reset.py`, and `scripts/inject_fault.sh` in its flag fallback |
| Secrets Manager `rm/flagsmith/server-key` | The SDK key `rm-router` and `rm-judge` initialise with. Remote evaluation normally uses the Development environment's client-side key (on its SDK Keys page; not a secret, already exists, but kept out of this public repo so nobody else spends the request cap). Task 1.2 confirmed that events accept this client-side key, so no server-side key is needed | `rm-router`, `rm-judge`, task 1.2's spike function, and `rm-answer-cheaper` under the section 9 fallback only; task 2.2's live test reads it for its identity-flags read and never prints it |

Each reader fetches the value at run time: scripts with the human's
credentials, functions with their own role. It keeps the value in memory and
never prints, logs or writes it. `agent/scripts/setup_gateway.py` passes the
organisation key straight into the credential provider; AgentCore Identity
keeps its own copy, which stops working once the Flagsmith key is deleted
after judging.

Agents never print, log, commit or echo a secret value. The human enters
both values. The organisation API key is named `rollout-marshal-agent`
in Flagsmith (created Nov 24, admin, no expiry; delete it after judging). SNS topic `rm-incidents` has one email subscription that the
human confirms. The incident address is the root variable
`incident_email`, marked sensitive because plans are attached to pull
requests in a public repo. It is kept with `invoker_stub` in the
git-ignored `infra/terraform.tfvars` that every plan and apply uses; the
lead copies it into each worktree it creates.

## 9. Fault injection

FIS experiment template `rm-delay-cheaper`: action
`aws:lambda:invocation-add-delay` on `rm-answer-cheaper`, 100 percent of
invocations, added delay 10000 ms, duration 6 minutes. The function needs
the FIS extension layer (its arm64 ARN is published in SSM), the S3
configuration bucket, the IAM policies and the environment variables from
the AWS FIS Lambda actions guide, with
`AWS_FIS_SLOW_POLL_INTERVAL_SECONDS=10` so the fault starts quickly. The
fault can take up to a minute to ramp and the alarm needs two full
one-minute periods, so expect ALARM three to five minutes in. Timebox:
2 hours. Fallback, built only if task 4.3's timebox runs out: a Flagsmith
boolean feature `chaos_delay_cheaper` that `rm-answer-cheaper` reads from
the environment flags, cached 30 seconds; a missing flag or a failed read
means off. When it is on, the function sleeps 10000 ms before the Bedrock
call, the same delay as the FIS fault. `scripts/inject_fault.sh --fallback`
gets or creates the flag (off), turns it on for 6 minutes, then off, and
also turns it off on exit; `scripts/reset.py` turns it off too.

## 10. Report

`s3://rm-reports-818928662830/runs/<run_id>/report.md`, sections in this
order: Hypothesis, Setup, Timeline, Results, Guardrails, Incident, Known
bias, Recommendation, Time taken. Known bias names two (Gate 1): rolled-back
users still count in the cheaper arm, and an identity first seen while
`assistant_cheaper_enabled` is off is enrolled in `cheaper` without ever
being served it.

## 11. Load

`scripts/load.py --run-id <run_id> --users N --questions M --url <api>`
runs from the shell of whichever agent's task calls for it, not as a Lambda (a full run outlasts Lambda's 15 minutes). At most 10
concurrent requests, questions from `app/questions.json` (30 support
questions for a fictional SaaS product). It stops at its count, refuses to
start while another load holds `rm-load.lock`, an OS file lock in the
repository's git common directory that every worktree shares and that frees
itself if the script dies, and never runs unattended.

| Use | Users x questions | Answers |
| --- | --- | --- |
| Task 3.6 smoke | 150 x 2 | 300 |
| Task 4.3 fault test | 150 x 2 | 300 |
| Full run (5.1, and each of the two further runs in 5.2) | 150 x 10 | 1,500 |

Budget for the event: 300 + 300 + 3 x 1,500 = 5,100 answers. Stop and ask the human before
exceeding 7,500. Every answer the load script sends counts toward that,
and each run's evidence records its sent count. After a failed 5.1 or 5.2
attempt, the lead rehearses at 150 x 2 until the path runs clean, before
the next counted 150 x 10 run.

Each request is one JSON `answer_request` (`question`, `identity`) posted
to `--url`, the full URL of the HTTP API's only route, `POST /ask`, as the
app module's output `ask_url` gives it. The route uses `AWS_IAM`
authorization, so `load.py` signs every request with SigV4 (service
`execute-api`) from the shell's AWS credentials: the repo is public, and an
unsigned URL that leaked into evidence could spend Bedrock money. A load
never prints the URL.

## 12. Repo layout

| Path | Owner | Holds |
| --- | --- | --- |
| `contracts/`, `infra/*.tf` (root, including module wiring and the `invoker_stub` variable), `Makefile`, `pyproject.toml`, `.gitignore`, `CLAUDE.md` (each edit approved by the human), `scripts/preflight.sh`, `scripts/e2e.py`, `openspec/`, `docs/REVIEW.md`, `docs/submission.md`, `evidence/gate-*` | Lead | names, schemas, root wiring, gates |
| `evidence/<task-id>-*` | the task's owner | one file per task that asks for one |
| `agent/`, `cli/`, `infra/modules/agent/` | A | Strands agent, handlers, state machine, tools, the `rm-flagsmith-admin` function (`agent/flagsmith_admin/`), Gateway setup script, CLI |
| `app/`, `infra/modules/app/`, `infra/spike/`, `scripts/load.py`, `scripts/bootstrap_flagsmith.py`, `scripts/seed_experiment.py`, `scripts/reset.py` | B | router, answer, judge, questions, prices, `rm-runs`, reports bucket |
| `signals/`, `infra/modules/signals/`, `infra/modules/fis/`, `scripts/inject_fault.sh` | C | invoker, alarm, rule, schedule, SNS, FIS |
| `docs/`, `README.md` | D | README, time saved, video script |

Root `.tf` files in `infra/` belong to the lead, one per module, added
before that module's first plan. Each file under `infra/modules/` belongs
to exactly one task. Nobody edits another owner's
path; they ask the lead instead.

## 13. Verify before relying

These came from documentation read during planning. The task named owns
proving or correcting each one and writes the result to `evidence/`; the
Result column records it. Task 1.4 filled in the rows owned by 1.2, 1.3 and
1.4.

| Claim | Task | Result |
| --- | --- | --- |
| Exact signatures of `get_identity_flags`, `track_exposure_event`, `track_event` and `flush_events`; that events survive a Lambda freeze; that an outcome event sent from a second invocation, with its own client, joins an exposure sent from the first (as `rm-judge` and `rm-router` will), shown on a spike experiment the lead created with the human's yes (`spike_flag`, `spike_metric` = mean of `spike_event`, 50/50, rollout 100 percent) using all 150 identities | 1.2 | verified: signatures in `evidence/1.2-sdk-spike.md`; every event was delivered before the handler returned; experiment 807 showed means 0.8077 and 0.1944, exactly what the second client sent (`evidence/1.4-spike-experiment.md`) |
| Request fields for thinking effort `low` on both answer models, and for disabling thinking on Haiku 5.5 and Sonnet 5. Fable 5.1 cannot disable thinking (AWS model card) | 1.2 | verified: `output_config.effort` and `thinking.type` `disabled`, each call `end_turn` with text; Fable 5.1 answered 400 to the disable |
| What remote evaluation reports as `flag.variant` for each arm, the control arm especially (`current`, `control` or nothing); an exposure without a label is not counted | 1.2 | corrected: `control` for the control arm (value `current`), `cheaper` for the treatment |
| That the MCP server can set a multivariate option's key; if not, the Admin API does it | 1.3 | verified: yes, through its option create (key read back) |
| That the MCP server can create a metric (added by 1.4) | 1.3 | corrected: no; its create_metric drops the required definition and Flagsmith answers 400, so metrics go through the Admin API tool |
| How `create_experiment` sets the rollout, the control's value and the option's 50 percent allocation, and what an experiment gets when they are left out | 1.3 | verified: all inside `experiment_rollout`, which Flagsmith requires (400 without it) |
| The API spellings of a metric's direction and of an experiment metric's expected direction | 1.3 | verified: `up`, `down`, `informational`; `increase`, `decrease`, `not_increase`, `not_decrease` |
| Whether the AgentCore CLI can add an MCP server target with API-key outbound auth | 1.3 | verified: no (CLI 1.3.0 refuses it); `agent/scripts/setup_gateway.py` uses the AWS API |
| How a feature's state changes in an environment with v2 feature versioning (added by 1.4) | 1.3 | verified: create a version, update its default state, publish it (four MCP calls, in `evidence/1.3-flagsmith-agent-spike.md`) |
| Whether Flagsmith event calls count toward the 50,000 request cap | 3.6 | open |
| p95 alarms on embedded-metric custom metrics with a dimension behave as designed | 4.1 | open |
| Admin API paths for starting an experiment and refreshing results | 1.3 | verified: `start/` with name and hypothesis; `results/refresh/` (202, then 429 inside 5 minutes); `results/` |
| Whether an experiment can be ended through any API | 1.3 | documented, not exercised: Flagsmith's published spec lists `complete/`; no agent calls it |
| Whether the feature is locked while its experiment runs, and what "locked" blocks | 1.3 | corrected: the API accepted all three writes 1.3 tried on a running experiment's feature (its option, state and description) and 1.3 put them back; the experiment's rollout read back unchanged |
| Gateway API-key credential provider sending `Authorization: Api-Key <key>`, and whether the Gateway adds the space after the prefix itself | 1.3 | corrected: the service stores the prefix trimmed (`Api-Key`) and calls authenticate with either value; that the Gateway adds the space is inferred, not observed |
| Metric aggregation types available (occurrence, count, sum, mean) | 1.3 | verified |
| Result fields returned by Flagsmith: which of lift, credible interval and win probability come back and in what units, and which one gives the probability that `cheaper` is not worse or the credible interval for lift | 1.4, from 1.2's spike experiment before it is ended (`evidence/1.4-results-sample.json`); 3.6 confirms with the real metrics; mapped in 2.2 | verified: `lift` (relative, a fraction), `ci_low` and `ci_high` (the dashboard shows the level, 95 percent), `chance_to_win`; no "not worse" field |
| An API read for exposures per variation and for request usage, so an agent can check them without the dashboard | 1.3 | verified for exposures (`exposures/`, per variation); verified for usage, with a lag: `usage-data/total-count/` read 0 eleven to 25 minutes after 1.2's 150 identity requests (task 1.3) and 150 at 01:10Z the next day (Gate 1), so a read lags by more than 25 minutes |
| FIS action parameter names and the extension layer ARN for arm64 in us-east-1 | 4.3 | open |
| Bedrock prices for the three models | 3.3 | open |
