<!-- Draft (task 6.1). Each [[source: what]] is a number or fact that a real run or the
Human's timed pass supplies, filled from the source it names before Gate 4. Nothing here is
estimated. Delete this comment once the last one is filled. -->

# Time saved

How long it takes an engineer to set up Rollout Marshal's experiment by hand in the Flagsmith
dashboard, against how long Rollout Marshal takes from `marshal start` to `RUNNING`.

## The two numbers

| | Time |
| --- | --- |
| **By hand**, in the Flagsmith dashboard, from the first click to the experiment running | [[6.1: manual setup, mm:ss]] |
| **With Rollout Marshal**, from `marshal start` to `RUNNING` | [[5.1: marshal start to RUNNING, mm:ss]] |
| of which the engineer reads the plan and approves it | [[5.1: review, plan returned to approve seen, mm:ss]] |
| of which the agent works: it plans, then launches | [[5.1: agent's own time, mm:ss]] |

The manual time comes from one timed pass by the Human, described below. The agent's time
comes from the UTC timestamps that `scripts/e2e.py` prints in task 5.1, recorded in
`evidence/5.1-e2e.md`.

Setting up by hand is not a one-off cost. Flagsmith allows one active experiment per feature,
and an ended experiment cannot restart, so every run needs a new feature and a new experiment
(design.md section 4). The manual steps repeat on every run, and so does the agent's launch.

## The agent's time, from task 5.1

Run [[5.1: run ID]]. `scripts/e2e.py` prints four UTC timestamps (tasks.md 5.1):

| `scripts/e2e.py` prints | UTC |
| --- | --- |
| it sends `start` | [[5.1: start sent, UTC]] |
| the plan returns | [[5.1: plan returned, UTC]] |
| it sees the approve | [[5.1: approve seen, UTC]] |
| the run reaches `RUNNING` | [[5.1: RUNNING, UTC]] |

From those:

- `marshal start` to `RUNNING` is `RUNNING` minus `start` sent: [[5.1: marshal start to RUNNING, mm:ss]].
- The review is the approve seen minus the plan returned: [[5.1: review, plan returned to approve seen, mm:ss]].
  That is the Human reading the plan and running `marshal approve <run_id> launch`.
- The agent's own time is the first minus the second: [[5.1: agent's own time, mm:ss]].

Inside that interval the agent does what design.md section 6 lists:

- **Plan.** The model writes the hypothesis and the summary. Code fills every other field of
  the plan from `contracts/names.py`: models, metrics, split, sample floor, stop rules,
  guardrails and the recommendation rule.
- **Launch**, after the approve:
  - enable the `rm-tick` schedule;
  - get or create the two permanent features;
  - set `assistant_cheaper_enabled` enabled;
  - get or create the run's feature `assistant_model_<run_id>`, its `cheaper` option and the
    experiment, through the Flagsmith MCP server;
  - get or create the three metrics, through the Admin API;
  - start the experiment, through the Admin API.

## The manual steps

These are the steps an engineer takes in the Flagsmith dashboard (project "Toggle Wars",
`55601`, environment "Development") to set up the same experiment. Each name and value is
design.md section 4's unless a note says otherwise. The steps follow the order of the agent's
launch, so the kill switch is on before the experiment starts.

In the timed pass, two things stand in for the real ones:

- `<scratch>` is a scratch run ID, [[lead: scratch run ID for 6.1, r<word><HHMM>]], following
  design.md section 4's pattern for scratch runs.
- `spike_kill_switch`, task 1.3's scratch boolean (feature `306318`), stands in for
  `assistant_cheaper_enabled`. The pass never touches `assistant_cheaper_enabled` or
  `assistant_default_model`.

| Step | In the dashboard | Lap |
| --- | --- | --- |
| 1. Kill switch | Open the kill switch and check that it is on; turn it on if it is off. In the pass, this is `spike_kill_switch`. Development uses v2 feature versioning, so a change is published as a new feature version (design.md section 4). | [[6.1: lap 1, mm:ss]] |
| 2. The run's feature | Create a multivariate feature `assistant_model_<scratch>`, enabled, with value `current`. The value is the control. | [[6.1: lap 2, mm:ss]] |
| 3. Its option | Add one variation to it, with value `cheaper` and key `cheaper`. An option without a key records no exposures. | [[6.1: lap 3, mm:ss]] |
| 4. The metrics | Check that the three environment metrics exist and match: `helpful_rate` is the mean of `answer_helpful`, higher is better; `mean_latency_ms` is the mean of `answer_latency_ms`, lower is better; `mean_cost_micro_usd` is the mean of `answer_cost_micro_usd`, lower is better. | [[6.1: lap 4, mm:ss]] |
| 5. The experiment | Create an experiment on `assistant_model_<scratch>`, named `assistant_model_<scratch>` as the agent names its experiments. Hypothesis: "Claude Sonnet 5 answers support questions as helpfully as Claude Fable 5.1, at no more latency or cost." Attach `helpful_rate` first, expected "should not decrease"; then `mean_latency_ms` and `mean_cost_micro_usd`, each expected "should not increase". Rollout: 100 percent of identities, the control `current`, `cheaper` at 50 percent. | [[6.1: lap 5, mm:ss]] |
| 6. Start | Start the experiment. The timer stops when its status shows running. | [[6.1: lap 6, mm:ss]] |
| | **Total** | [[6.1: manual setup, mm:ss]] |

Notes on the steps:

- Step 4 is a check, as the agent's get-or-create is. Task 2.2's live test created the three
  metrics environment-wide (IDs 966, 967 and 968, `evidence/2.2-live.md`), so every run finds
  them. If one is missing or differs, the Human stops and tells the lead. The pass creates no
  metric and changes none.
- The hypothesis in step 5 is the first sentence of the one `scripts/seed_experiment.py`
  uses (design.md section 5).
- Step 5's rollout block decides the split, whatever the option's own default allocation
  says (design.md section 4).

## The method

1. **Before the timer** (not timed):
   - No run is active: `uv run marshal status` prints a message that starts "no active run"
     and names no run. (A pointer left by `scripts/seed_experiment.py` is named there;
     `scripts/reset.py` clears it.)
   - No load is running.
   - The lead gives the Human the scratch run ID.
   - The Human opens the dashboard at the Development environment of "Toggle Wars".
2. **The timed pass.** The Human performs steps 1 to 6 once, working from this list, with a
   stopwatch and a lap at the end of each step. The timer starts at the first click of step 1,
   and stops when the experiment's status shows running.
   - Pass: [[6.1: date and UTC start of the pass]].
   - Scratch experiment ID: [[6.1: scratch experiment ID]].
   - Where the dashboard's order or forms differ from the list, the Human writes what
     differed: [[6.1: dashboard differences, if any]]. Two points are untested in the
     dashboard: whether its variation form takes a key (step 3), and whether its experiment
     form takes the rollout block (step 5). Both were set through the Admin API for task
     1.2's spike experiment, and through MCP in task 1.3.
3. **After the timer** (not timed):
   - The Human ends the scratch experiment in the dashboard: End Experiment, then "Roll out
     control" in the "Complete experiment" dialog, as the spike experiments were ended
     (`evidence/1.4-spike-experiment.md`).
   - The lead reads its status afterwards through the Admin API, and records it here:
     [[6.1: status read after the end]].
   - The scratch feature and its experiment go on the cleanup list in
     `evidence/gate-1-review.md`.

The pass sends no traffic. The router reads only the active run's feature (design.md
section 4), and with no active run it reads `assistant_default_model`. So the scratch
experiment changes nothing that is served. It makes no Bedrock call and no SDK flag request.

## What the comparison leaves out

- **The manual number is a lower bound.** The Human works from this list, with every name and
  value already chosen. An engineer starting from the same prompt would first have to decide
  them: the metrics, their directions, the split, the rollout and the hypothesis. The agent's
  number includes that planning.
- **The agent's number includes the review.** It is the whole interval from `marshal start`
  to `RUNNING`, the Human's reading of the plan included. The table above shows that part on
  its own line.
- **Neither number counts what comes after the start.** That is watching latency, turning the
  kill switch off when the cheaper model slows, checking it is healthy before resuming,
  reading results and writing the report. The agent does those too (design.md section 6),
  but this file times setup only.
- **Neither counts one-time setup.** The two permanent features and the three metrics exist
  once per environment; both the agent and the engineer find them on every later run.

## Difference

By hand minus with Rollout Marshal: [[6.1: difference, manual minus agent, mm:ss]].
