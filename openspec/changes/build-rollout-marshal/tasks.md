# Tasks: Build Rollout Marshal

Read `design.md` first. Every name and value comes from there.

How to read a task:

- **Owner**: `Lead` is the integration lead; `A`, `B`, `C`, `D` are the
  workstream agents; `Human` is Ahmed.
- **Needs**: what must be merged, and where it says "applied", what the
  lead must have applied, before the task starts.
- **Pass**: the check that decides done. A task is not done until its Pass
  check is green AND a goldfish reviewer has approved it (docs/REVIEW.md).
  A line marked **Human** inside a Pass check is done by Ahmed; the agent
  asks, waits, and records his answer in the evidence file.
- Order inside a task: failing test, code, Pass check, pull request into
  `dev`. One pull request per task.
- A Pass line that starts "After ... applied" is a post-merge check. The
  pull request merges on the earlier lines; the box is ticked only when
  every line is green. A task may have one follow-up pull request that
  holds only its post-merge lines, reviewed by a fresh goldfish under the
  deployed-state rule; the box is ticked when it merges. One owner may put
  several tasks' follow-ups in one pull request.
- Evidence files go in `evidence/<task-id>-<name>` and include the exact
  commands that produced them.
- Timebox: 90 minutes unless the task says otherwise.
- Workstream agents never run `terraform apply`. They ask the lead. The one
  exception is B's `infra/spike/` in task 1.2 (design.md section 2).
- A build agent starts only the scratch experiments a task names (1.3, 2.2,
  3.6, 4.3); any other start, such as 1.2's spike experiment, needs the
  human's yes. The human ends every experiment; no agent ends one
  (CLAUDE.md).

After section 1, workstreams A, B and C run in parallel against the stubs
their tasks name, not against each other.

## 0. Human prerequisites (before 2026-11-26T02:30Z)

- [x] 0.1 **Human**: a shell where `aws sts get-caller-identity` prints
  account `818928662830`, with credentials that last the whole session.
- [x] 0.2 Bedrock data retention mode is `aws_review` in us-east-1 in that
  account. Ahmed approved this setting on Nov 24, so if preflight finds
  any other mode the lead sets it (`bedrock:PutAccountDataRetention`,
  us-east-1 only) and records that it did.
- [x] 0.3 **Human**: secrets `rm/flagsmith/api-key` (the organisation API
  key `rollout-marshal-agent`) and `rm/flagsmith/server-key` (see
  design.md section 8) hold their values in that account's Secrets
  Manager.
- [x] 0.4 **Human**: the incident email address is in the kickoff prompt.
  A plus-address on an existing mailbox is enough, for example
  `you+rollout-marshal@yourdomain`.
- [x] 0.5 **Human**: registered on WeMakeDevs with Track 2 selected.
- [x] 0.6 (already done, nothing to do) The Development environment's managed warehouse is connected in
  Flagsmith (Environment Settings, Warehouse). It was on Nov 23; preflight
  does not check it, so 1.2 confirms the first event arrives.
- [x] 0.7 **Human**: `gh auth status` succeeds with push rights to
  `abebars/rollout-marshal`; `terraform`, `python3.13`, `uv` or `pip`, the
  AWS CLI and the AgentCore CLI are installed.

## 1. Foundation (blocks everything)

- [x] 1.0 **Bootstrap repo, branches and Terraform root**
  - Owner: Lead. Needs: section 0.
  - Files: everything in the handover as it is, `pyproject.toml`,
    `Makefile`, `scripts/preflight.sh`, `infra/backend.tf`,
    `infra/main.tf`, `infra/variables.tf`, empty
    `infra/modules/{agent,app,signals,fis}/`.
  - Do: run `scripts/preflight.sh`; commit the handover to `main` as the
    first commit; create `dev` from it; create the state bucket
    `rm-tfstate-818928662830`; `terraform init`.
  - `scripts/preflight.sh` prints one `ok` or `FAIL` line each for: UTC
    time is at or after `2026-11-26T02:30:00Z`; caller account is
    `818928662830`; data retention mode is `aws_review` (set it if not,
    per 0.2); both secrets
    exist and have a current version (never read the value); one Converse
    call succeeds for each of the three model IDs; AgentCore control plane
    and FIS APIs answer in us-east-1; `gh auth status`; `terraform
    version`; AgentCore CLI version.
  - Pass: every preflight line is `ok`; `git log --format=%cI main | tail
    -1` is at or after `2026-11-26T02:30:00Z`; `terraform init` succeeds.
    Goldfish review of this commit happens before it is pushed.

- [x] 1.1 **Contracts package**
  - Owner: Lead. Needs: 1.0.
  - Files: `contracts/names.py`, `contracts/schemas/{invoke,response,plan,
    run_item,results,judge_payload}.json`,
    `contracts/tests/test_contracts.py`.
  - Produces: one constant per name in design.md sections 3 to 9; the six
    schemas; `make test`, `make lint`.
  - Pass: `make test` green; a test asserts that every state, action,
    error code and feature key in design.md appears in `names.py`.
  - Gate: **Human approves this pull request** before 1.2 and 1.3 start.

- [ ] 1.2 **Spike: Bedrock and Flagsmith SDK from Lambda**
  - Owner: B. Needs: 1.1. B's first apply waits for the 1.1 gate's
    contract change to merge, and B's first Flagsmith call for the spike
    experiment to be running. Timebox: 2 hours, plus waiting for the
    warehouse.
  - Files: `app/spike/handler.py`, `infra/spike/main.tf` (its own tiny
    root with local state, applied and later destroyed by B under the
    guards in design.md section 2), `evidence/1.2-sdk-spike.md`.
  - The spike experiment, before B's first Flagsmith call, in the
    Development environment. The lead creates and starts it through the
    Admin API after this contract change merges; the human said yes to this
    launch on 2026-11-26. Feature `spike_flag` (value `current`, one
    variation keyed `cheaper` with value `cheaper`); metric `spike_metric`
    (mean of event `spike_event`, higher is better); an experiment on
    `spike_flag` with rollout 100 percent, split 50/50, and `spike_metric`
    as primary metric with expected direction "should not decrease".
  - Do: one function, `rm-spike-sdk` (arm64, a timeout of a few minutes),
    that reads the SDK key from `rm/flagsmith/server-key` at run time.
    - Converse: effort `low` on Fable 5.1 and on Sonnet 5 at `maxTokens`
      2000; thinking disabled on Haiku 5.5 at `maxTokens` 200, with no
      effort field; thinking disabled on Sonnet 5 as well. Fable 5.1 cannot
      disable thinking; record that, with the AWS model card link.
    - Invocation 1, the router's role: one client calls
      `get_identity_flags` for `user-001` to `user-150`, then
      `track_exposure_event` when the flag reports the identity in the
      experiment, then `flush_events()`. It records `flag.value` and
      `flag.variant` for an identity in each arm, and returns every
      identity's arm.
    - Invocation 2, the judge's role: a new client sends one `spike_event`
      per identity through `track_event`, with numeric values that differ
      by arm, taken from invocation 1's assignments so that it makes no
      identity requests; then `flush_events()`. Record each arm's expected
      mean.
  - Pass: the evidence file records each Converse response, with latency,
    token counts and `stopReason` `end_turn`; the exact SDK signatures
    used; how a numeric value travels on an event; the request fields that
    set effort `low` and that disable thinking, per model, each with a
    response that came back with text; what `flag.variant` says for each
    arm; and an API read (or, failing that, a **Human** dashboard check)
    showing the Development warehouse has received at least one event,
    with how long it took. The lead then refreshes and reads the
    experiment's results through the Admin API into
    `evidence/1.4-results-sample.json`.
    **Human**, after that read: both variations show at least 50
    identities and each arm's `spike_metric` mean matches what B sent.
    The human then ends the experiment, and the file records the status
    read afterwards. `infra/spike` is destroyed before the review. If no
    results arrive within the timebox (warehouse wait included), B records
    what did arrive, and the
    join moves to 3.6 only with the human's written acceptance at Gate 1.

- [ ] 1.3 **Spike: agent to Flagsmith**
  - Owner: A. Needs: 1.1. A's first deploy or Flagsmith call waits for the
    1.1 gate's contract change to merge. Timebox: 2 hours.
  - Files: `agent/spike/agent.py`, `agent/scripts/setup_gateway.py`, the
    AgentCore CLI project under `agent/`,
    `evidence/1.3-flagsmith-agent-spike.md`.
  - Do: a hello-world Strands agent deployed as `rm_agent` on AgentCore
    Runtime, and the Gateway pieces of design.md section 7 (`rm-gateway`,
    `rm-gateway-role`, `rm-flagsmith-api-key`, target `flagsmith`),
    created by the idempotent `setup_gateway.py`. The agent calls the MCP
    tools by name, not through the model, to create:
    - feature `assistant_model_rspike<HHMM>`, a new key on every attempt
      (`experiment_feature_key(scratch_run_id("spike", now))`; if that key
      already exists, wait for the next UTC minute), value `current`, one
      option keyed `cheaper` at 50 percent;
    - metric `spike_mcp_metric`, mean of `spike_event`, higher is better;
    - one experiment, rollout 100 percent, with `spike_mcp_metric`
      expected "should not decrease".

    A local script starts the experiment through the Admin API. Then try
    to change the feature while it runs, create the boolean feature
    `spike_kill_switch` and toggle it while the experiment runs, and read
    status, exposures and results. No agent calls an end-experiment endpoint.
  - Pass: the evidence file answers every row of design.md section 13
    owned by 1.3, with the request and response for each; the row on
    ending an experiment through an API is "documented, not exercised",
    citing the Flagsmith pages. It also lists the Gateway's MCP tool names
    and every feature key created. **Human** ends every spike experiment
    started; the file records the status read afterwards.
  - Past 90 minutes without working Gateway auth: switch to the direct
    MCP fallback (design.md section 7) and record it.

- [ ] 1.4 **Reconcile contracts with the spikes**
  - Owner: Lead. Needs: 1.2, 1.3.
  - Do: a contract-change pull request updating design.md and
    `contracts/` wherever a spike contradicted them; every section 13 row
    owned by 1.2, 1.3 or 1.4 marked `verified` or `corrected`; the row on
    ending an experiment through an API stays `documented, not exercised`.
    If 1.2's join moved to 3.6, split that row: the 1.2 parts `verified`,
    the join a new row owned by 3.6.
    `evidence/1.4-results-sample.json`, read during 1.2, records which of
    lift, credible interval and win probability Flagsmith returns, and in
    what units; 2.2's results mapping starts from it. 1.3's Admin API call
    times check `rm-flagsmith-admin`'s 30-second timeout.
  - Pass: `make test` green; no such row left open. If the sample has no
    lift and no credible interval, the **Human** decides at Gate 1: keep a
    promote path, or accept that every clean run recommends hold.
    **Gate 1** runs here.

## 2. Workstream A: experiment agent

- [ ] 2.1 **State store and state machine**
  - Needs: 1.1. Files: `agent/state.py`, `agent/tests/test_state.py`.
  - Produces, with every pointer change inside the run-item write's
    transaction (design.md section 6): `load_run(run_id) -> Run`;
    `create_run(prompt, plan) -> Run`, one transaction with the pointer and
    `notified` empty (refuses with `RUN_ACTIVE` naming the active run, or
    `BAD_REQUEST` when the run ID is in use); `claim(run_id, expected_from,
    to) -> Run` (raises `StaleState`); `resume(run_id, expected_from, to,
    seen_pending_at) -> Run`, the takeover; `commit(run_id, expected_from,
    to, actor, reason, updates, claimed_at) -> Run`, which carries the launch commit's
    `feature_key` and the terminal commit's `clear_active`;
    `release(run_id, claimed_at)`; `get_active() -> Pointer | None`.
  - Tests: every row of the transition table claims and commits (the
    `(none)` row through `create_run`); every other pair raises
    `IllegalTransition`; two concurrent claims from the same state, exactly
    one wins; `resume` refuses a `pending` younger than 360 seconds;
    two takeovers of the same stale `pending` produce one winner, and the
    stalled owner's commit then fails; a released claim is taken over at
    once; `history` grows by exactly one per transition; `create_run` with
    a pointer present raises `RUN_ACTIVE` naming it, and with the run item
    present (same minute) `BAD_REQUEST`; a terminal commit while the
    pointer names another run is cancelled and changes nothing; a terminal
    commit with no pointer succeeds; a colliding transaction, and a claim
    that fails with `TransactionConflictException` against an in-flight
    commit, are each treated as a lost race.
  - Pass: `pytest agent/tests/test_state.py` green against moto or
    DynamoDB Local.

- [ ] 2.2 **Flagsmith tools**
  - Needs: 1.4. Files: `agent/tools/flagsmith.py`,
    `agent/flagsmith_admin/handler.py`,
    `infra/modules/agent/flagsmith_admin.tf`,
    `agent/tests/test_flagsmith_tools.py`.
  - Produces (all get-or-create or set-to-value, so safe to repeat):
    `ensure_permanent_features()`, `ensure_experiment(run_id, plan) ->
    {feature_key, experiment_id, metric_ids}`, `start_experiment(id)`,
    `refresh_results(id)`, `get_results(id) -> Results` (the
    `results.json` shape), `experiment_status(id) -> str`,
    `set_kill_switch(enabled)`, `set_default_model(value)`.
  - Tests: unit tests with a fake MCP client assert the exact tool names
    and arguments recorded in `evidence/1.3-*.md`, taking run IDs from the
    recorded arguments rather than literals; calling each `ensure_*` twice
    creates once; a found metric or permanent feature that differs from its
    definition (design.md section 4) stops with an error; `ensure_experiment`
    sets rollout 100 and the `cheaper` option at 50; `get_results` maps
    `evidence/1.4-results-sample.json` onto `results.json` and computes
    `ready` (49 and 50 identities, one null mean), finished against
    `evidence/3.6-results.json` after 3.6 (that one test waits; the rest
    does not); `refresh_results` inside 5 minutes of the last one does not
    call the API.
  - Pass: unit tests green; the plan from `infra/` lists this task's
    resources to add and destroys nothing outside them.
    After `infra/modules/agent` is applied: `pytest -m live` creates and starts an
    experiment on run ID `rlive<HHMM>` and reads it back; **Human** ends
    it; the ID is in the pull request.

- [ ] 2.3 **Planner**
  - Needs: 1.1. Files: `agent/planner.py`, `agent/tests/test_planner.py`.
  - Produces: `make_plan(prompt) -> Plan`, valid against `plan.json`. The
    model writes the hypothesis and wording. Metric names, split, sample
    floor (50 per variation), alarm name, guardrail limits and the
    recommendation rule are injected from `contracts.names`.
  - Tests (model call recorded and replayed): the sample prompt in
    KICKOFF.md yields a valid plan; a prompt naming no model move, or any
    move other than Fable 5.1 to Sonnet 5, yields `BAD_REQUEST` before any
    write; injected values equal the constants even when the prompt asks
    for others; the model call sends thinking effort `low` and no sampling
    parameter; the planner's model call reports the move it read, and code
    compares it with design.md section 3. Record one plan call and one
    report-length call, each with latency, output tokens and `stopReason`.
    If the report call comes near 4,000 tokens or botocore's 60-second read
    timeout, tell the lead, who adjusts `AGENT_MAX_TOKENS` or the agent's
    Bedrock read timeout through a contract change.
  - Pass: `pytest agent/tests/test_planner.py` green.

- [ ] 2.4 **AWS tools: signals, probe, schedule, notify, report**
  - Needs: 1.1. Files: `agent/tools/{signals,probe,schedule,notify,
    report}.py`, tests alongside.
  - Produces: `alarm_state(name) -> "OK" | "ALARM" | "INSUFFICIENT_DATA"`
    and the alarm's threshold, from one DescribeAlarms call;
    `probe_cheaper(threshold_ms, n=5) -> {passed, max_ms, reason}`, the
    invokes in parallel, each sending the `answer_request` with
    `PROBE_QUESTION` as `PROBE_IDENTITY`; `set_tick(enabled)`;
    `notify(run_id, subject, body, dedupe_key) -> message_id`;
    `write_report(run, results) -> key` (a plain put); `guardrails_pass(
    results) -> bool`; `recommend(run, results) -> "promote" | "hold" |
    "hold, re-run"`, where an incident is a non-empty `incidents`.
  - Tests: `recommend` covers the three branches, including an incident
    run that would otherwise qualify for promote, and the fallback rule
    when `prob_cheaper_not_worse` is null (lower bound at minus 5 percent
    passes, just below fails); `guardrails_pass` at
    exactly 110 percent latency passes and just above fails; the report
    has the nine sections in order; the probe fails when any invoke is at
    or above the threshold, raises, times out, returns a function error or
    a reply that fails `answer_response`;
    `notify` with a repeated `dedupe_key` publishes once and returns the
    same message ID, and a failed publish records nothing;
    `set_tick(False)` when already disabled is a no-op.
  - Pass: unit tests green with moto.

- [ ] 2.5 **Action handlers**
  - Needs: 2.1, 2.2, 2.3, 2.4. Files: `agent/handlers.py`,
    `agent/app.py`, `agent/tests/test_handlers.py`.
  - Produces: `handle(payload) -> dict` for every action in design.md
    section 7, applying each transition by claim, effects, commit.
  - Tests, with fakes for every tool: one per row of the transition table,
    asserting the effects in order; `start` while a run is active returns
    `RUN_ACTIVE`; `approve`, `decline`, `check` in a state where they are
    not legal return `ILLEGAL_IN_STATE` with no effect; `alarm` and `tick`
    with no active run, or with nothing to do, return a no-op with
    `error: null`; `alarm` twice rolls back and notifies once; `tick` in
    `RUNNING` checks the alarm before results; `tick` in `ROLLED_BACK`
    with ready results does not move to `RESULTS_READY`; `tick` in
    `ROLLED_BACK` with a failing probe changes nothing and leaves no
    `pending`; `tick` in `ROLLED_BACK` with the alarm in ALARM skips the
    probe; a failed DescribeAlarms read in `ROLLED_BACK` or
    `AWAITING_RESUME` counts as a failing probe; a `tick` or a repeat
    during a fresh claim runs no effects; `approve` or `check` on a
    `PLANNED` run with no pointer returns `ILLEGAL_IN_STATE` and changes
    nothing, while `decline` ends it; `tick` in `RUNNING` inside 600 seconds of `first_running_at`
    does not read results; `check` in `RUNNING` with the alarm in ALARM
    rolls back exactly as `alarm` does; `check` in every non-terminal state
    does what `tick` would, and in a terminal state returns
    `ILLEGAL_IN_STATE`; an `alarm`, `tick`, `check` or `approve` in
    `RESULTS_READY` with the alarm in ALARM rolls back and adds an
    `incidents` entry, and after resume the new report recommends `hold,
    re-run`; an `alarm` stamped before the latest entry into `RUNNING` is a
    no-op; a rollback after a resume sends a second note and adds a second
    `incidents` entry, while a resumed transition whose publish already went
    out does not publish again; `approve` in `AWAITING_RESUME` with the
    alarm in ALARM returns the status; `decline` from `ROLLED_BACK` leaves
    the kill switch off; `approve`, `decline` or `check` on a run the
    pointer does not name returns `RUN_ACTIVE`; a pointer whose run item is
    missing is a no-op for `status`, `tick` and `alarm`; `status` with no
    active run returns nulls and "no active run"; `decline` while a
    transition is pending returns `TRANSITION_PENDING`; a planner model
    error on `start` returns `UPSTREAM_FAILED` and creates nothing; a
    failed launch ran `set_tick(True)` first, and a `tick`, or separately a
    `check`, completes it with one history entry; a `tick` right after
    `EFFECT_FAILED` resumes at once (the claim was released); `start`,
    `decline`, `start` within one minute returns `BAD_REQUEST`;
    `status` returns the run without changing it; an unknown `run_id`
    returns `NOT_FOUND`; `decline` of a `PLANNED` run touches nothing in
    Flagsmith; `approve` in
    `AWAITING_RESUME` with a failing probe goes back to `ROLLED_BACK` and
    adds no `incidents` entry, and a resumed rollback adds no second entry;
    `approve` in `RESULTS_READY` with a running experiment returns
    `EXPERIMENT_NOT_ENDED`; an effect that throws returns `EFFECT_FAILED`, leaves `pending` set,
    and a later `tick` resumes and completes it; every terminal state clears
    the pointer and disables the tick; every payload and response
    validates against its schema.
  - Pass: `pytest agent/tests/test_handlers.py` green.

- [ ] 2.6 **CLI**
  - Needs: 1.1. Files: `cli/marshal.py`, `cli/tests/test_cli.py`.
  - Produces: `marshal start "<prompt>"`, `approve <run_id>`,
    `decline <run_id>`, `status [run_id]`, `check <run_id>`; one payload
    per call, a new session ID each time, through the `InvokeAgentRuntime`
    client setting shared with `rm-invoker` (read timeout 290 seconds, one
    attempt); prints state, message, what it waits for, any `pending`
    target and any error code. `main()` in `cli/marshal.py` returns an exit
    code; when this task merges, the lead adds the console script
    `marshal = cli.marshal:main` to `pyproject.toml`.
  - Pass: unit tests with a fake runtime client green.

- [ ] 2.7 **Deploy the agent**
  - Needs: 2.5, 2.6; 3.5 applied (for `rm-runs`).
  - Files: what the AgentCore CLI generates under `agent/`,
    `infra/modules/agent/runtime_role.tf`, `agent/scripts/setup_gateway.py`
    (started in 1.3, finished here), `evidence/2.7-agent-deploy.md`.
  - After each `agentcore deploy`, A writes the runtime ARN to
    `/rm/agent-runtime-arn` (for example `put-parameter --overwrite` in a
    small deploy script). A pre-merge apply of `runtime_role.tf` is allowed
    (design.md section 2).
  - Pass: `marshal status` against the deployed runtime prints "no active
    run"; SSM parameter `/rm/agent-runtime-arn` holds the ARN (the
    evidence shows `get-parameter`); the evidence file lists the Gateway
    ID, its targets and the role's permissions; the plan from `infra/`
    lists this task's resources to add and destroys nothing outside them.

## 3. Workstream B: assistant API

- [ ] 3.1 **Answer functions**
  - Needs: 1.4. Files: `app/answer/handler.py`, tests.
  - Produces: one handler deployed twice, `rm-answer-current` and
    `rm-answer-cheaper`, model from env `MODEL_ID`. Input `{question,
    identity}`; output `{answer, input_tokens, output_tokens, model_id}`.
  - Tests: input and output validate against `answer_request` and
    `answer_response`; no sampling parameters are sent; `maxTokens` and the
    thinking setting match the contract and the fields 1.2 recorded;
    `stopReason` `max_tokens` raises; a Bedrock throttle returns a typed
    error. The `chaos_delay_cheaper` read (design.md section 9) is built
    only if 4.3's FIS timebox runs out.
  - Pass: unit tests green.

- [ ] 3.2 **Router**
  - Needs: 1.4. Files: `app/router/handler.py`, tests.
  - Produces: the evaluation order in design.md section 4; synchronous
    invoke of the answer function; latency measured around it;
    `AnswerLatencyMs` with `Variant` = served variant; async invoke of
    `rm-judge` with the `judge_payload.json` shape.
  - Tests, with a fake Flagsmith client: kill switch off serves `current`
    to an identity assigned `cheaper`; no pointer, or a pointer without
    `feature_key`, serves `assistant_default_model` and records nothing;
    exactly one identity request per call with an active run, and
    `get_experiment_flag` is never called; the exposure is tracked only
    when the flag reports the identity in the experiment; `flush_events()`
    runs before return; the pointer is cached 30 seconds; an answer-function error returns 502 and still
    emits latency; with no active run, `rm-judge` is not invoked; the judge
    payload's `assigned` is the flag's value, while the exposure sends
    `flag.variant`; every metric carries exactly the `Variant` dimension
    set.
  - Pass: unit tests green.

- [ ] 3.3 **Judge**
  - Needs: 1.4. Files: `app/judge/handler.py`, `app/prices.json`, tests.
  - Produces: one judge call returning `{"helpful": true|false}`; events
    `answer_helpful` (value 1 or 0), `answer_latency_ms`,
    `answer_cost_micro_usd`, one of each per answer; CloudWatch `AnswerCostMicroUsd`,
    `AnswerHelpful`.
  - Tests: cost arithmetic against `prices.json` per `model_id`; an
    unparseable judge reply counts as not helpful and is logged; with a
    fake client, all three events are sent and `flush_events()` is called
    before the handler returns; thinking is disabled on the judge call,
    with the request fields 1.2 recorded; every metric carries exactly the
    `Variant` dimension set; `prices.json` carries a source URL and date.
  - Pass: unit tests green.

- [ ] 3.4 **Load script**
  - Needs: 1.1. Files: `scripts/load.py`, `app/questions.json`, tests.
  - Produces: `scripts/load.py --users N --questions M --url <api>`; at
    most 10 concurrent; prints sent, ok, failed; lock file refuses a
    second start.
  - Tests: sends exactly N x M; never exceeds the concurrency; refuses
    while another load holds `rm-load.lock`, an OS file lock in the git
    common directory (`git rev-parse --path-format=absolute
    --git-common-dir`), including a second start from a different working
    directory; the lock frees itself when the script dies.
  - Pass: unit tests green.

- [ ] 3.5 **App infrastructure, bootstrap, seed and reset**
  - Needs: 3.1, 3.2, 3.3; before its apply review, the lead's
    `rm-review-readonly` role (applied from `infra/review.tf`, its
    `simulate-principal-policy` result recorded) and the human's `rm-review`
    profile. Files: `infra/modules/app/*.tf`,
    `scripts/bootstrap_flagsmith.py`, `scripts/seed_experiment.py`,
    `scripts/reset.py`.
  - Produces: HTTP API, `rm-router`, `rm-answer-current`,
    `rm-answer-cheaper`, `rm-judge`, table `rm-runs`, the reports bucket,
    IAM. The module takes inputs `cheaper_extra_layers` and
    `cheaper_extra_env` and outputs the cheaper function's name and role
    name, so the FIS module can attach to it without editing B's files. `bootstrap_flagsmith.py` get-or-creates the two permanent
    features. `seed_experiment.py <run_id>` creates and starts an
    experiment (rollout 100, the `cheaper` option at 50, from the
    constants) and writes the pointer with `feature_key`, without the
    agent; it refuses when a pointer exists and sets the kill switch
    enabled, as launch does. `reset.py` clears the pointer, enables the
    kill switch, sets `assistant_default_model` to `current`, turns
    `chaos_delay_cheaper` off, disables the tick, and lists any experiment
    still running for the human to end; it refuses while the pointer names
    a run in a non-terminal state, unless given `--force`.
  - Pass: `terraform validate` clean, and the plan from `infra/` lists this
    task's resources to add and destroys nothing outside them. After 3.5 is
    merged and applied: the lead attaches the plan and apply output, and a
    read-only goldfish check finds the API, the four functions, `rm-runs`
    and the reports bucket.

- [ ] 3.6 **Deployed smoke**
  - Needs: 3.5 applied, 3.4. Files: `evidence/3.6-app-smoke.md`.
  - Do: `bootstrap_flagsmith.py`, `seed_experiment.py rsmoke<HHMM>`,
    `load.py --users 150 --questions 2`; after the human ends the
    experiment, `reset.py`.
  - Pass, read through the API found in 1.3 (or **Human** on the
    dashboard if none exists): both variations show at least 50
    identities; CloudWatch has `AnswerLatencyMs` datapoints for both
    variants; the baseline p95 for exactly `Variant=cheaper`, the Flagsmith
    request count before and after (to see whether events count), the raw
    results response (`evidence/3.6-results.json`, with whether lift,
    interval and win probability come back, in what units and at what
    interval level), the answers
    sent, and the cost check are recorded. The cost check is each
    variant's Sum and SampleCount of `AnswerCostMicroUsd` since
    2026-11-26T02:30:00Z, and a projection: spent so far plus the
    remaining answers, with one 150 x 2 rehearsal per expected failure,
    times the higher mean cost per answer. If the projection passes
    60,000,000 or the answers would pass 7,500, the lead asks the human
    before the next load. If 1.2's
    join moved here, both variations also show non-null values for all
    three metrics. **Human** ends the smoke experiment.

## 4. Workstream C: signals and fault

- [ ] 4.1 **Signals infrastructure**
  - Needs: 4.2 merged (for the invoker's code). Files:
    `infra/modules/signals/*.tf`.
  - Produces: `rm-invoker` (env `STUB=1`), alarm `rm-cheaper-latency-p95`,
    the EventBridge rule on ALARM, schedule `rm-tick` (disabled), SNS
    topic `rm-incidents` with the email subscription.
  - The module declares `alarm_threshold_ms` with no default; the lead's
    root fills it from `contracts/names.json`. The tick schedule's input is
    the tick payload, `{"action": "tick"}`.
  - Pass: `terraform validate` clean, and the plan from `infra/` lists
    this task's resources to add and destroys nothing outside them.
    After 4.1 is applied: **Human** confirms the SNS subscription email
    and `aws sns list-subscriptions-by-topic` shows it confirmed; one
    `AnswerLatencyMs` datapoint above the threshold, with `Variant=cheaper`
    and unit Milliseconds, published once a minute for 3 minutes in a row,
    drives the alarm to ALARM, with no alarm setting changed
    (`EvaluateLowSampleCountPercentile` stays at its default, evaluate); the
    test's start and end times, the time to ALARM and the time from the last
    datapoint back to OK are recorded in `evidence/4.1-alarm.md`, outside
    3.6's baseline window. After 3.6: C
    gives the lead the baseline p95, and the lead opens the contract-change
    pull request with the threshold rule of design.md section 5; it records
    B, T, the alarm margin (B + 10000 - T) and the headroom (T - B).

- [ ] 4.2 **Invoker**
  - Needs: 1.1. Files: `signals/invoker/handler.py`, tests.
  - Produces: maps an alarm state-change event to the `alarm` payload and
    a schedule event to `tick`; reads the runtime ARN from
    `/rm/agent-runtime-arn`; calls `InvokeAgentRuntime` with a new session
    ID. With `STUB=1` it only logs the payload.
  - Tests: both mappings validate against `invoke.json`; an OK or
    INSUFFICIENT_DATA event sends nothing; a transport failure raises (so
    Lambda retries the asynchronous event) but a well-formed agent response with a non-null
    `error` does not.
  - Pass: unit tests green. After 4.1 is applied: `aws cloudwatch
    set-alarm-state` to ALARM logs one valid payload, saved to
    `evidence/4.2-invoker.md`.

- [ ] 4.3 **Fault injection**
  - Needs: 3.6; 4.1 applied with the recalibrated threshold. Timebox: 2 hours. Files: `infra/modules/fis/*.tf`,
    `scripts/inject_fault.sh`, `evidence/4.3-fis.md`.
  - Do: seed a run (`seed_experiment.py rfault<HHMM>`), start
    `load.py --users 150 --questions 2`, then start the fault. The lead
    applies `infra/modules/fis` first.
  - Pass: the alarm reaches ALARM within 5 minutes of the fault starting
    and leaves ALARM after it ends; times recorded. **Human** ends the
    experiment, then `reset.py` runs. A pre-merge apply of
    `infra/modules/fis` is allowed (design.md section 2), and the plan from
    `infra/` lists this task's resources to add and destroys nothing outside
    them. Past the timebox: ask the lead to have B add the
    `chaos_delay_cheaper` flag read to `app/answer/handler.py`, and record
    why.

## 5. Integration (Lead)

- [ ] 5.1 **Join the workstreams: first full run**
  - Needs: 2.7, 3.6, 4.1 applied with the recalibrated threshold, 4.2,
    4.3. Gate 2 passed.
  - Files: `scripts/e2e.py`, `evidence/5.1-e2e.md`.
  - Do: `scripts/reset.py`, then set `invoker_stub = false` and apply.
    `scripts/e2e.py` drives one run, takes the load size as an argument,
    prints UTC timestamps when it sends `start`, when the plan returns, when
    it sees the approve and when the run reaches `RUNNING`, and never
    approves anything itself; its wait for `AWAITING_RESUME` allows for the
    alarm's return to OK that 4.1 records. It runs `marshal start`, then
    pauses for the **Human** to run `marshal approve`; starts
    `load.py --users 150 --questions 10` and injects the fault straight
    away; waits for `ROLLED_BACK`, then `AWAITING_RESUME`; pauses for the
    **Human** to approve the resume; waits for `RESULTS_READY`; pauses for
    the **Human** to decline, or to end the experiment in Flagsmith and
    approve. It prints exactly what to do at each pause.
  - Pass: the run item's `history` reads `PLANNED, RUNNING, ROLLED_BACK,
    AWAITING_RESUME, RUNNING, RESULTS_READY`, then `HELD` or `PROMOTED`;
    the evidence file holds that history, the four timestamps, the
    answers sent, the report's S3 key, and the SNS message IDs from the run
    item's `notified`. **Gate 3** runs here.

- [ ] 5.2 **Two more clean runs**
  - Needs: 5.1.
  - Pass: `scripts/reset.py` then `scripts/e2e.py`, twice, with no code
    change between; both histories saved under `evidence/5.2/`. If a run
    fails, fix, rehearse at 150 x 2 until the path runs clean (as long as
    4.3 shows that load fires the alarm), and the count restarts. Every answer counts toward the 7,500 stop.

## 6. Workstream D: proof and submission

- [ ] 6.1 **Time saved**
  - Needs: 5.1. Files: `docs/time-saved.md`.
  - Do: list the manual dashboard steps to set up the same experiment.
    **Human** performs them once, timed, on scratch names only while no run
    is active, never the two permanent features, and ends the scratch
    experiment afterwards. The agent's time, `marshal start` to `RUNNING`
    with the review time shown inside it, comes from 5.1's e2e timestamps.
  - Pass: both numbers, the step list and the method are in the file.

- [ ] 6.2 **README**
  - Needs: 5.1, 6.1. Files: `README.md`.
  - Must contain: the problem; the architecture; setup steps a stranger
    can follow; how Flagsmith is used (MCP, Admin API, SDK,
    Experimentation); the timed numbers; the three approval points; and a
    plain list of what is simulated (synthetic users, injected fault,
    model-scored helpfulness, the rollback bias).
  - Pass: a goldfish given only the README states what the project does
    and names every simulated part.

- [ ] 6.3 **Video script and shot list**
  - Needs: 5.2, 6.1. Files: `docs/video-script.md`.
  - Six beats: the ask, the plan and approval, the setup, the failure,
    the rollback and resume, the decision. Problem stated in the first 15
    seconds. Ends on the two timed numbers.
  - Pass: **Human** approves it.

- [ ] 6.4 **Submission**
  - Owner: Human, checklist kept by Lead. Files: `docs/submission.md`.
  - Needs: Gate 4 passed and `dev` merged to `main`.
  - Pass: repo public, README final on `main`, video uploaded, entry
    submitted on WeMakeDevs. Target `2026-11-29T02:00:00Z`; hard cutoff
    `2026-11-29T14:30:00Z`.

## Spec scenario coverage

| Spec scenario | Tested in |
| --- | --- |
| experiment-agent: all scenarios | 2.5 (2.1 for the race, 2.4 for the recommendation) |
| assistant-api: variant routing, request budget | 3.2 |
| assistant-api: events flushed, judge reply unparseable | 3.3 |
| assistant-api: bounded load | 3.4 |
| signals: alarm wakes the agent | 4.2 |
| signals: tick from launch to a terminal state / run ends | 2.5 (launch enables the tick first; terminal states disable it) |
| signals: manual fallback | 2.5 (`check`) |
| signals: repeatable fault | 4.3 |

## Definition of done

- [ ] One prompt produces a plan and waits for approval.
- [ ] The agent creates the flag, metrics and experiment through the
  Flagsmith MCP server and starts it through the Admin API.
- [ ] An injected fault triggers an automatic rollback and an incident
  email.
- [ ] Resume and promotion each wait for a human decision.
- [ ] A decision report with Flagsmith results exists in S3.
- [ ] Three clean runs (5.1 plus two in 5.2).
- [ ] README and video done, `dev` merged to `main`, entry submitted.
