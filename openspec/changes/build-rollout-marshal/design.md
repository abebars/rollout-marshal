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
| Infra as code | Terraform in `infra/` for everything except the agent runtime, which the AgentCore CLI deploys, and the Gateway pieces in section 7, which `agent/scripts/setup_gateway.py` creates |
| Terraform state | S3 backend, bucket `rm-tfstate-818928662830`, key `rollout-marshal/terraform.tfstate`, created by task 1.0. Only the integration lead runs `terraform apply`, after each infra merge, and attaches the plan output to the pull request. Workstream agents run `terraform validate` and `terraform plan` only. The one exception is task 1.2, below |
| Timebox | 90 minutes per task unless the task says otherwise. Past it, stop and tell the lead |
| Flagsmith | SaaS, organisation 41656, project "Toggle Wars" (ID `55601`), environment "Development" |
| Flagsmith plan limits | Free: 50,000 API requests a month, 1 project, 1 seat |
| Repo | `github.com/abebars/rollout-marshal`, public |

Task 1.2's exception to the apply rule: agent B applies and destroys
`infra/spike/`, a separate root whose local state is never committed. B runs
the account check first. Its provider pins `us-east-1` and allows only this
account, as the root does. Every resource name starts with `rm-spike-`. The
spike function gets the secret's name, never its value, and reads the value
at run time. The plan, apply and destroy output go in the task's evidence
file, and the destroy runs before the pull request is reviewed.

## 3. Bedrock models

| Role | Model ID | Notes |
| --- | --- | --- |
| Current arm (control) | `us.anthropic.claude-fable-5-1` | Needs account data retention mode `aws_review` in us-east-1. Send no `temperature`, `top_p` or `top_k`. Thinking is always on. |
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
- Agent prose: `maxTokens` 4000.
- A response with `stopReason` `max_tokens` is an error, never a result.

Task 1.2 proves the exact request fields for effort `low` on both answer
models, and for disabling thinking on the judge's model (Haiku 5.5) and on
Sonnet 5, and records them. Fable 5.1 cannot disable thinking; Bedrock
rejects the request. Lambda timeouts: answer functions 25 seconds, router 29
seconds, judge 30 seconds.

## 4. Flagsmith contract

| Thing | Name or value |
| --- | --- |
| Run ID | `r` + UTC `YYYYMMDDHHMM`, e.g. `r202611271530`. Scratch runs made by spikes and tests use `r` + a word + UTC `HHMM`, e.g. `rsmoke0412`. Pattern for both: `^r[a-z]*[0-9]{4,12}$` |
| Experiment feature (multivariate) | `assistant_model_<run_id>` |
| Control | the feature's own value, `current`. Flagsmith reserves the control; it is not a keyed option |
| Treatment | one multivariate option with key `cheaper` and value `cheaper`, at 50 percent. An option without a key records no exposures |
| Experiment rollout | 100 percent of identities, set when the experiment is created |
| Kill-switch feature (boolean) | `assistant_cheaper_enabled`, default enabled |
| Events, one of each per answer | `answer_helpful` (value 1 or 0, integer), `answer_latency_ms` (value), `answer_cost_micro_usd` (value) |
| Metrics (environment level, get-or-create) | `helpful_rate` = mean of `answer_helpful` (primary, higher is better); `mean_latency_ms` = mean of `answer_latency_ms` (lower is better); `mean_cost_micro_usd` = mean of `answer_cost_micro_usd` (lower is better) |
| Expected direction per experiment metric | `helpful_rate`: should not decrease; `mean_latency_ms`: should not increase; `mean_cost_micro_usd`: should not increase |
| Identity format | `user-001` to `user-150` |
| SDK | `flagsmith` Python package 6.6.0 or later, remote evaluation, `enable_events=True` |
| Flags call | `get_identity_flags`, once per answer |
| Exposure call | `track_exposure_event(feature, identifier, value=flag.variant, metadata={"experiment_id": flag.experiment.id})`, only when `flag.experiment.in_experiment` |
| Outcome call | `track_event`, with the identity and a numeric `value` |
| Flush | `flush_events()` before every handler returns. Buffer settings alone do not survive a Lambda freeze |

Available metric aggregations are occurrence, count, sum and mean. There
is no "rate"; occurrence counts an identity once, which is useless at 10
answers per identity, so helpfulness is a mean of 1s and 0s.

The SDK rows above come from a read of the SDK source and docs during
review and are proven by task 1.2 before anything depends on them.

Rules:

- One experiment per run. Flagsmith allows one active experiment per
  feature and an ended experiment cannot restart, so each run gets a new
  feature key.
- The agent creates features, multivariate options, metrics and the
  experiment through the Flagsmith MCP server (`https://mcp.flagsmith.com`,
  header `Authorization: Api-Key <key>`).
- The MCP server cannot start an experiment or refresh results. Those go
  through the Flagsmith Admin API from a small Lambda tool.
- Ending an experiment is done by the engineer in the Flagsmith dashboard.
  The flag stays locked until then. No agent ends an experiment or calls an
  end-experiment endpoint.
- One Flagsmith identity request per answer. Flag, identity, trait and
  environment-document requests count toward the 50,000 cap.
- Get-or-create checks what it finds. A found metric must match its
  definition on event, aggregation and direction. A found permanent feature
  is checked on type only: a standard feature, not multivariate, and the
  default-model feature holds a string value. Its current state and value
  are never compared, because rollback and promote change them; the defaults
  apply only on create. A mismatch stops with an error for the human to fix
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
   seconds), serve that model, record no exposure and no event.
2. Call `get_identity_flags` once. Both the experiment feature and the
   kill switch are read from that one result. Do not call
   `get_experiment_flag`; it makes its own identity request.
3. If the experiment feature reports the identity is in the experiment,
   record the exposure with `track_exposure_event`.
4. If `assistant_cheaper_enabled` is disabled, serve `current` whatever the
   variation says, and set `served=current`, `assigned=<variation>`.
5. Otherwise serve the assigned variation.
6. `flush_events()` before returning.

Results can be refreshed at most once every 5 minutes. The run item keeps
`last_refresh_at`; a tick inside that window reads the last results
without refreshing.

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
4.1 recalibrates it to twice the measured baseline p95, rounded up to the
next 1000, and records the number here.

## 6. State

DynamoDB table `rm-runs`, partition key `pk` (string).

Pointer item: `pk = "ACTIVE"`, attributes `run_id` and, once launched,
`feature_key`. Written at `PLANNED` (without `feature_key`), given its
`feature_key` when launch commits, removed when the run reaches a terminal
state. The router treats a pointer without `feature_key` as "no active
run". The router caches the pointer for 30 seconds, so for up to 30
seconds after a terminal state it may still serve the old run; that is
accepted.

Run item: `pk = "RUN#<run_id>"` with:

| Attribute | Type | Meaning |
| --- | --- | --- |
| `run_id` | S | |
| `state` | S | One of the states below |
| `prompt` | S | The engineer's request |
| `plan` | M | Hypothesis, metrics, split, stop rules (schema `contracts/schemas/plan.json`) |
| `feature_key` | S | `assistant_model_<run_id>` |
| `experiment_id` | N | Flagsmith experiment ID |
| `metric_ids` | M | name to Flagsmith metric ID |
| `incident` | M | `started_at`, `cleared_at`, `alarm_name`; absent if none |
| `report_key` | S | S3 key of the report |
| `history` | L | Appended on every transition: `at`, `from`, `to`, `actor`, `reason` |
| `pending`, `pending_at` | S | Target state of a claimed, unfinished transition, and when it was claimed |
| `last_refresh_at` | S | When results were last refreshed |
| `first_running_at` | S | When the run first entered `RUNNING` |
| `created_at`, `updated_at` | S | ISO 8601 UTC |

States and the only legal transitions:

| From | To | Trigger | Actor |
| --- | --- | --- | --- |
| (none) | `PLANNED` | `start` | engineer |
| `PLANNED` | `RUNNING` | `approve` | engineer |
| `PLANNED` | `ABORTED` | `decline` | engineer |
| `RUNNING` | `ROLLED_BACK` | `alarm`; or `tick` or `check` when the alarm is in ALARM | agent |
| `RUNNING` | `RESULTS_READY` | `tick` or `check`, when the alarm is not in ALARM and results are ready | agent |
| `RUNNING` | `HELD` | `decline` | engineer |
| `ROLLED_BACK` | `AWAITING_RESUME` | `tick` or `check`, when the health probe passes | agent |
| `ROLLED_BACK` | `HELD` | `decline` | engineer |
| `AWAITING_RESUME` | `RUNNING` | `approve`, when the health probe passes again | engineer |
| `AWAITING_RESUME` | `ROLLED_BACK` | `approve`, when the health probe fails | agent |
| `AWAITING_RESUME` | `HELD` | `decline` | engineer |
| `RESULTS_READY` | `PROMOTED` | `approve`, only if the experiment is ended in Flagsmith | engineer |
| `RESULTS_READY` | `HELD` | `decline` | engineer |

Terminal states: `PROMOTED`, `HELD`, `ABORTED`.

Rules that hold everywhere:

- **One run at a time.** `start` is refused while any run is in a
  non-terminal state. The pointer is written at `PLANNED` with the
  condition `attribute_not_exists(pk)`, so a second `start` fails on it.
  `clear_active` is a conditional delete on `run_id`.
- **`tick` and `check` in `RUNNING` look at the alarm first, then results.**
- **`check` is legal in `RUNNING` and `ROLLED_BACK` only**, where it does
  what a `tick` would do. Anywhere else it returns `ILLEGAL_IN_STATE`.
- **Results are not read in the first 600 seconds after
  `first_running_at`.** This keeps a fast-filling experiment from reaching
  `RESULTS_READY` before a fault can be shown.
- **Results are only read in `RUNNING`.** A tick in `ROLLED_BACK` or
  `AWAITING_RESUME` never moves a run to `RESULTS_READY`.
- **`alarm` or `tick` with no active run, or in a state where it has
  nothing to do, is a successful no-op**: response `message` says why, no
  error, no side effect. Only `start`, `approve`, `decline` and `check`
  return an error when not legal in the current state.
- **An alarm in `AWAITING_RESUME`, `ROLLED_BACK` or `RESULTS_READY` is a
  no-op.** The kill switch is already off or the run is over.

How a transition is applied (guard, claim, effect, commit):

0. Guard: checks that decide WHETHER to move run before any claim and
   change nothing if they fail: the health probe, "results are ready",
   and "the experiment is ended in Flagsmith". A failed guard returns the
   current status (or `EXPERIMENT_NOT_ENDED`) and leaves no `pending`.
   The one exception is `approve` in `AWAITING_RESUME` with a failing
   probe, which is itself a transition to `ROLLED_BACK`.

1. Claim: conditional write that sets `pending = <to>` and
   `pending_at = <now>`, on condition `state = <from>` and `pending` is
   absent. If the condition fails, another invocation owns it: re-read and
   return the current status. Do nothing else.
2. Effects: run the side effects below. Every effect is idempotent
   (get-or-create by key, set-to-value, publish with a deduplication key
   stored on the item).
3. Commit: write `state = <to>`, remove `pending`, append to `history`.

If an effect throws, leave `pending` in place and return an error. Any
later `tick`, `check` or repeat of the same action that finds `pending`
older than 60 seconds resumes from step 2 for that same target state.

What each transition does:

- `(none) -> PLANNED`: write the run item and the `ACTIVE` pointer.
- `PLANNED -> RUNNING` (launch): get-or-create the two permanent features;
  set `assistant_cheaper_enabled` enabled; get-or-create the run's
  feature, options, metrics and experiment through the MCP server; start
  the experiment through the Admin API tool; enable the tick schedule.
- `RUNNING -> ROLLED_BACK`: disable `assistant_cheaper_enabled`; record
  `incident.started_at`; publish the incident note to SNS.
- `ROLLED_BACK -> AWAITING_RESUME` (guard: the health probe passes; the
  probe is 5 direct invokes of `rm-answer-cheaper` and passes if the
  slowest is under the alarm threshold): record `incident.cleared_at`;
  publish a resume request to SNS.
- `AWAITING_RESUME -> RUNNING` (guard: the probe passes again): enable
  `assistant_cheaper_enabled`.
- `AWAITING_RESUME -> ROLLED_BACK` (the probe failed on `approve`):
  publish a note saying the cheaper model is slow again.
- `RUNNING -> RESULTS_READY` (guard: results refreshed through the Admin
  API tool show both variations with at least 50 identities and non-null
  values): write the report to S3; publish it to SNS.
- `RESULTS_READY -> PROMOTED` (guard: the experiment is ended): set
  `assistant_default_model` to `cheaper`; clear the pointer; disable the
  tick schedule. From then on the router serves `cheaper` to everyone.
- Any transition into `HELD`: clear the pointer; disable the tick
  schedule; set `assistant_cheaper_enabled` enabled; leave everything else
  in Flagsmith as it is. `assistant_default_model` is not changed, so the
  router keeps serving `current`.
- `PLANNED -> ABORTED`: clear the pointer. Nothing in Flagsmith is
  touched, because nothing was created.

Guardrails pass when the cheaper arm's `mean_latency_ms` is at most 110
percent of the current arm's, and its `mean_cost_micro_usd` is not higher
than the current arm's.

Recommendation rule (in code, not in the prompt):

- An incident happened during the run: recommend `hold, re-run`, and state
  that rolled-back users still count in the cheaper arm.
- Otherwise recommend `promote` only if the probability that `cheaper` is
  not worse on `helpful_rate` is at least 0.95 and both guardrails pass.
- Otherwise recommend `hold`.

Flagsmith's docs show a win probability against control and a credible
interval for lift; a "not worse" probability may not exist. Task 3.6
records a real results response. If there is no such field,
`prob_cheaper_not_worse` is null and the promote test becomes: the lower
bound of the credible interval for the lift on `helpful_rate` is at or
above minus 5 percent, and both guardrails pass. Task 2.2's results
mapping is finished against that recorded response.

## 7. Invoking the agent

One AgentCore Runtime agent, `rm_agent`. Each invocation is short and
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
"error": null | {"code": str, "message": str}}`.
Error codes: `ILLEGAL_IN_STATE`, `RUN_ACTIVE`, `NOT_FOUND`,
`EXPERIMENT_NOT_ENDED`, `EFFECT_FAILED`, `BAD_REQUEST`.

Results shape used by the tools (`contracts/schemas/results.json`):
`{"ready": bool, "variations": {"current": V, "cheaper": V},
"prob_cheaper_not_worse": float | null,
"helpful_lift_ci_lower": float | null}` where
`V = {"identities": int, "helpful_rate": float | null,
"mean_latency_ms": float | null, "mean_cost_micro_usd": float | null}`.
Task 2.2 maps Flagsmith's result fields onto this shape from the response
recorded in task 3.6. Schemas in `contracts/schemas/`: `invoke`,
`response`, `plan`, `run_item`, `results`, `judge_payload`.

Three things invoke it:

| Caller | Sends | How |
| --- | --- | --- |
| `marshal` CLI (`cli/`) | `start`, `approve`, `decline`, `status`, `check` | `InvokeAgentRuntime` with the engineer's AWS credentials |
| `rm-invoker` Lambda | `alarm` | EventBridge rule on the alarm entering ALARM. Reads the runtime ARN from SSM parameter `/rm/agent-runtime-arn`. Terraform variable `invoker_stub` (root, set by the lead) sets env `STUB`; with `STUB=1` it only logs the payload |
| `rm-tick` schedule | `tick` | EventBridge Scheduler, every 2 minutes, enabled only while a run is active; targets `rm-invoker` |

`check` is the manual fallback for the alarm path: it does whatever `alarm`
or `tick` would do for the run's current state.

The model is used for three things only: turning the prompt into a plan,
writing the incident note, and writing the report prose. State changes,
stop rules and the recommendation are deterministic code.

Agent tools:

| Tool | Reaches | Through |
| --- | --- | --- |
| Flagsmith MCP tools | features, multivariate options, metrics, experiments, results | AgentCore Gateway MCP target, API key from AgentCore Identity |
| `flagsmith_admin` | start experiment, refresh results, read experiment status | Lambda `rm-flagsmith-admin` as a Gateway target |
| `state` | `rm-runs` | boto3 in the agent |
| `signals` | alarm state, CloudWatch metric statistics | boto3 in the agent |
| `probe` | 5 direct invokes of `rm-answer-cheaper` | boto3 in the agent |
| `schedule` | enable or disable the `rm-tick` schedule | boto3 in the agent |
| `notify` | SNS topic `rm-incidents` | boto3 in the agent |
| `report` | S3 `rm-reports-818928662830` | boto3 in the agent |

AgentCore pieces. `agent/scripts/setup_gateway.py` gets or creates each one
by name, so it is safe to repeat; the CLI's gateway commands may be used
instead under the same names:

| Piece | Name | Notes |
| --- | --- | --- |
| Runtime | `rm_agent` | One AgentCore CLI project under `agent/`, deployed from task 1.3 on. Task 2.7 redeploys the same runtime, so its ARN never changes |
| Gateway | `rm-gateway` | Protocol `MCP`, inbound auth `AWS_IAM`, so no Cognito pool. The agent signs with SigV4 through `mcp-proxy-for-aws`, and the runtime's role gets `bedrock-agentcore:InvokeGateway` on the Gateway |
| Gateway service role | `rm-gateway-role` | Also the outbound auth of the Lambda target, which supports only the service role |
| Credential provider | `rm-flagsmith-api-key` | Holds the organisation key from `rm/flagsmith/api-key`, passed in the `apiKey` field of `CreateApiKeyCredentialProvider`, never through the CLI's env file |
| MCP server target | `flagsmith` | `https://mcp.flagsmith.com`, API-key outbound auth, header `Authorization`, prefix `Api-Key ` |
| Lambda target | `flagsmith-admin` | Lambda `rm-flagsmith-admin`, added by task 2.2 |

A target's name shapes the tool names the Gateway gives its tools, so these
names never change. Stack and role names that the CLI generates without the
`rm-` prefix are accepted.

Fallback if Gateway auth to the Flagsmith MCP server is not working after
90 minutes of task 1.3: connect the agent directly to `https://mcp.flagsmith.com`
with the API key header and record the decision in `evidence/`.

## 8. Secrets and notifications

| Name | Holds | Read by |
| --- | --- | --- |
| Secrets Manager `rm/flagsmith/api-key` | Flagsmith organisation API key (MCP and Admin API) | Gateway credential provider, `rm-flagsmith-admin`, and every local script or test that calls Flagsmith's MCP server or Admin API directly: task 1.3's probes, `agent/scripts/setup_gateway.py`, live tests, `scripts/bootstrap_flagsmith.py`, `scripts/seed_experiment.py`, `scripts/reset.py`, and `scripts/inject_fault.sh` in its flag fallback |
| Secrets Manager `rm/flagsmith/server-key` | The SDK key `rm-router` and `rm-judge` initialise with. Remote evaluation normally uses the Development environment's client-side key (on its SDK Keys page; not a secret, already exists, but kept out of this public repo so nobody else spends the request cap). Task 1.2 confirms whether events need a server-side key instead; if so the human creates one under SDK Keys | `rm-router`, `rm-judge`, task 1.2's spike function, and `rm-answer-cheaper` under the section 9 fallback only |

Each reader fetches the value at run time: scripts with the human's
credentials, functions with their own role. It keeps the value in memory and
never prints, logs or writes it. `agent/scripts/setup_gateway.py` passes the
organisation key straight into the credential provider; AgentCore Identity
keeps its own copy, which stops working once the Flagsmith key is deleted
after judging.

Agents never print, log, commit or echo a secret value. The human enters
both values. The organisation API key is named `rollout-marshal-agent`
in Flagsmith (created Nov 24, admin, no expiry; delete it after judging). SNS topic `rm-incidents` has one email subscription that the
human confirms.

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
2 hours. Fallback: a Flagsmith boolean feature `chaos_delay_cheaper` that
`rm-answer-cheaper` reads and sleeps on.

## 10. Report

`s3://rm-reports-818928662830/runs/<run_id>/report.md`, sections in this
order: Hypothesis, Setup, Timeline, Results, Guardrails, Incident, Known
bias, Recommendation, Time taken.

## 11. Load

`scripts/load.py --users N --questions M --url <api>` runs from the shell
of whichever agent's task calls for it, not as a Lambda (a full run outlasts Lambda's 15 minutes). At most 10
concurrent requests, questions from `app/questions.json` (30 support
questions for a fictional SaaS product). It stops at its count, refuses to
start if a lock file shows another load running, and never runs
unattended.

| Use | Users x questions | Answers |
| --- | --- | --- |
| Task 3.6 smoke | 150 x 2 | 300 |
| Task 4.3 fault test | 150 x 2 | 300 |
| Full run (5.1, and each of the two further runs in 5.2) | 150 x 10 | 1,500 |

Budget for the event: 300 + 300 + 3 x 1,500 = 5,100 answers. Stop and ask the human before
exceeding 7,500.

## 12. Repo layout

| Path | Owner | Holds |
| --- | --- | --- |
| `contracts/`, `infra/*.tf` (root, including module wiring and the `invoker_stub` variable), `Makefile`, `pyproject.toml`, `.gitignore`, `CLAUDE.md` (each edit approved by the human), `scripts/preflight.sh`, `scripts/e2e.py`, `openspec/`, `docs/REVIEW.md`, `docs/submission.md`, `evidence/gate-*` | Lead | names, schemas, root wiring, gates |
| `evidence/<task-id>-*` | the task's owner | one file per task that asks for one |
| `agent/`, `cli/`, `infra/modules/agent/` | A | Strands agent, handlers, state machine, tools, the `rm-flagsmith-admin` function (`agent/flagsmith_admin/`), Gateway setup script, CLI |
| `app/`, `infra/modules/app/`, `infra/spike/`, `scripts/load.py`, `scripts/bootstrap_flagsmith.py`, `scripts/seed_experiment.py`, `scripts/reset.py` | B | router, answer, judge, questions, prices, `rm-runs`, reports bucket |
| `signals/`, `infra/modules/signals/`, `infra/modules/fis/`, `scripts/inject_fault.sh` | C | invoker, alarm, rule, schedule, SNS, FIS |
| `docs/`, `README.md` | D | README, time saved, video script |

Each `.tf` file belongs to exactly one task. Nobody edits another owner's
path; they ask the lead instead.

## 13. Verify before relying

These came from documentation read during planning and have not been
exercised. The task named owns proving or correcting each one, and writes
the result to `evidence/`.

| Claim | Task |
| --- | --- |
| Exact signatures of `get_identity_flags`, `track_exposure_event`, `track_event` and `flush_events`; that events survive a Lambda freeze; that an outcome event sent from a second invocation, with its own client, joins an exposure sent from the first (as `rm-judge` and `rm-router` will). 1.2 shows the join on a spike experiment the human creates and starts (`spike_flag` with one variation keyed `cheaper`, metric `spike_metric` = mean of `spike_event`, 50/50, rollout 100 percent), using all 150 identities. If no results arrive within 1.2's timebox, the join moves to 3.6, but only with the human's written acceptance at Gate 1 | 1.2 |
| Request fields for thinking effort `low` on both answer models, and for disabling thinking on Haiku 5.5 and Sonnet 5. Fable 5.1 cannot disable thinking (AWS model card) | 1.2 |
| What remote evaluation reports as `flag.variant` for each arm, the control arm especially (`current`, `control` or nothing); an exposure without a label is not counted | 1.2 |
| That the MCP server can set a multivariate option's key; if not, the Admin API does it | 1.3 |
| How `create_experiment` sets the rollout, the control's value and the option's 50 percent allocation, and what an experiment gets when they are left out. If MCP cannot set them, 1.4 adds an Admin API step to `rm-flagsmith-admin` | 1.3 |
| The API spellings of a metric's direction and of an experiment metric's expected direction | 1.3 |
| Whether the AgentCore CLI can add an MCP server target with API-key outbound auth | 1.3 |
| Whether Flagsmith event calls count toward the 50,000 request cap | 3.6 |
| p95 alarms on embedded-metric custom metrics with a dimension behave as designed | 4.1 |
| Admin API paths for starting an experiment and refreshing results | 1.3 |
| Whether an experiment can be ended through any API: documented, not exercised, because no agent ends an experiment | 1.3 |
| Whether the feature is locked while its experiment runs, and what "locked" blocks | 1.3 |
| Gateway API-key credential provider sending `Authorization: Api-Key <key>`, and whether the Gateway adds the space after the prefix itself | 1.3 |
| Metric aggregation types available (occurrence, count, sum, mean) | 1.3 |
| Result fields returned by Flagsmith: which of lift, credible interval and win probability come back and in what units, and which one gives the probability that `cheaper` is not worse or the credible interval for lift | 1.4, from 1.2's spike experiment before it is ended (`evidence/1.4-results-sample.json`); 3.6 confirms with the real metrics; mapped in 2.2 |
| An API read for exposures per variation and for request usage, so an agent can check them without the dashboard | 1.3 |
| FIS action parameter names and the extension layer ARN for arm64 in us-east-1 | 4.3 |
| Bedrock prices for the three models | 3.3 |
