<!-- Draft (task 6.1). The dashboard pass is filled in. Each remaining [[5.1: what]] comes
from task 5.1's run (evidence/5.1-e2e.md) before Gate 4. Delete this comment once the last
one is filled. -->

# Time saved

How long it takes to set up Rollout Marshal's experiment by clicking through the Flagsmith
dashboard, against how long Rollout Marshal takes from `marshal start` to `RUNNING`.

## The two numbers

| | Time |
| --- | --- |
| **In the Flagsmith dashboard**, from the first click to the experiment running, clicked through by the lead agent in Chrome at Ahmed's instruction, not by a person | about 2:18 |
| **With Rollout Marshal**, from `marshal start` to `RUNNING` | [[5.1: marshal start to RUNNING, mm:ss]] |
| of which the engineer reads the plan and approves it | [[5.1: review, plan returned to approve seen, mm:ss]] |
| of which the agent works: it plans, then launches | [[5.1: agent's own time, mm:ss]] |

**Who did the dashboard pass.** Task 6.1 asks the Human to do it. Ahmed told the lead to do it
instead (chat, 2026-11-27: "use chrome to test that and add any human gaps needed. don't ask
me for tasks until all project is completed"). So the lead agent drove the Flagsmith
dashboard in Chrome. The dashboard number is not a person's time: it includes the lead
agent's own time between actions, and a person might be faster or slower.

**What it covers.** The dashboard's experiment form attached only the primary metric, so the
dashboard experiment had one metric where Rollout Marshal's has three. Details are under
"What the pass found" below.

The agent's time comes from the UTC timestamps that `scripts/e2e.py` prints in task 5.1,
recorded in `evidence/5.1-e2e.md`.

Setting up is not a one-off cost. Flagsmith allows one active experiment per feature, and an
ended experiment cannot restart, so every run needs a new feature and a new experiment
(design.md section 4). The dashboard steps repeat on every run, and so does the agent's
launch.

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
  - get or create the run's feature `assistant_model_<run_id>` and its `cheaper` option
    through the Flagsmith MCP server;
  - get or create the three metrics through the Admin API;
  - create the experiment through the MCP server, with all three metrics attached;
  - start the experiment through the Admin API.

## The dashboard steps

These are the steps that set up the same experiment in the Flagsmith dashboard (project
"Toggle Wars", `55601`, environment "Development"), in the order of the agent's launch, so
the kill switch is on before the experiment starts. Each name and value is design.md
section 4's unless a note says otherwise. Each lap is approximate, from UTC timestamps taken
between steps.

In the pass, two things stood in for the real ones:

- The scratch run ID was `rmanual0851`, following design.md section 4's pattern for scratch
  runs, so the run's feature was `assistant_model_rmanual0851`.
- `spike_kill_switch`, task 1.3's scratch boolean (feature `306318`), stood in for
  `assistant_cheaper_enabled`. The pass touched neither `assistant_cheaper_enabled` nor
  `assistant_default_model`.

| Step | In the dashboard | Lap |
| --- | --- | --- |
| 1. Kill switch | Open the kill switch and check that it is on; turn it on if it is off. In the pass this was `spike_kill_switch`, already on. | about 0:06 |
| 2. The run's feature | "New Feature": a feature named `assistant_model_rmanual0851`, enabled, with the control value `current`. | about 0:35 |
| 3. Its option | In the same form, one variant with value `cheaper`, renamed from "Variant_1" to `cheaper`: the variant's name is its key. An option without a key records no exposures. | in lap 2, same form |
| 4. The metrics | Check that the three environment metrics exist and match: `helpful_rate` is the mean of `answer_helpful`, higher is better; `mean_latency_ms` is the mean of `answer_latency_ms`, lower is better; `mean_cost_micro_usd` is the mean of `answer_cost_micro_usd`, lower is better. All three were present, with the right events and directions. | about 0:12 |
| 5. The experiment | The experiment form, on `assistant_model_rmanual0851`, with a name and the hypothesis. It attaches one metric, the primary: `helpful_rate`, expected "Should not decrease". Rollout 100 percent of identities, split 50/50. | about 1:19 |
| 6. Start | "Create" in the confirm dialog, which creates the experiment and starts it. The timer stopped when its status showed Running. | about 0:06 |
| | **Total**, 08:51:57Z to 08:54:15Z | **about 2:18** |

Notes on the steps:

- Step 4 is a check, as the agent's get-or-create is. Task 2.2's live test created the three
  metrics environment-wide (IDs 966, 967 and 968, `evidence/2.2-live.md`), so every run finds
  them. The pass created no metric and changed none.
- The hypothesis for step 5 is the first sentence of the one `scripts/seed_experiment.py`
  uses (design.md section 5): "Claude Sonnet 5 answers support questions as helpfully as
  Claude Fable 5.1, at no more latency or cost."
- The experiment's split decides the arms, whatever the option's own default weight says
  (design.md section 4).

## What the pass found

The dashboard differed from the step list in five ways:

1. **The option's key.** The variant's name is its key: "Variant_1" was renamed to
   `cheaper` (step 3).
2. **One metric.** The experiment's create form attaches one metric only, the primary
   (`helpful_rate`, expected "Should not decrease"). `mean_latency_ms` and
   `mean_cost_micro_usd` cannot be attached there. The agent attaches all three in its
   experiment create through the MCP server, after getting or creating them through the
   Admin API.
3. **Defaults.** The rollout, 100 percent, and the 50/50 split were the form's defaults
   (step 5).
4. **Create starts it.** "Create Experiment" both creates the experiment and starts it, so
   steps 5 and 6 merge. Lap 6 is the confirm dialog's "Create".
5. **The option's weight.** Its default weight was left at 0 percent; the experiment's split
   decides.

## The method, as run on 2026-11-27

1. **Before the timer** (not timed):
   - No run was active: `marshal status` printed "no active run" at 08:51:49Z.
   - The scratch run ID was `rmanual0851`.
2. **The timed pass.** The lead agent did steps 1 to 6 once, from this list, in the
   Flagsmith dashboard in Chrome, at Ahmed's instruction (above). It took UTC timestamps
   between steps. The timer started at 08:51:57Z, at the first click of step 1, and stopped
   at 08:54:15Z, when the experiment's status showed Running.
   - The scratch experiment was 1172.
3. **After the timer** (not timed):
   - The lead ended experiment 1172 in the dashboard at 08:54:43Z with "Roll out control",
     which the dialog pre-selected ("No variant has a clear lead yet").
   - The dashboard then showed "Completed, Control rolled out to everyone". The lead read
     that status on the dashboard, not through the Admin API.
   - That end was done under the same instruction. Otherwise CLAUDE.md has the human end
     every experiment.
   - `assistant_model_rmanual0851` and experiment 1172 go on the cleanup list in
     `evidence/gate-1-review.md`.

The pass sent no traffic. The router reads only the active run's feature (design.md
section 4), and with no active run it reads `assistant_default_model`. So the scratch
experiment changed nothing that is served. It made no Bedrock call and no SDK flag request.

## What the comparison leaves out

- **The dashboard number is an agent's, not a person's.** It includes the lead agent's own
  time between actions. A person might be faster or slower.
- **It covers less than the agent's launch.** Every name and value was chosen before the
  pass began, while an engineer starting from the same prompt would first decide them: the
  metrics, their directions, the split, the rollout and the hypothesis. The agent's number
  includes that planning. And the dashboard experiment had only the primary metric, where
  the agent's has all three.
- **The agent's number includes the review.** It is the whole interval from `marshal start`
  to `RUNNING`, the Human's reading of the plan included. The table above shows that part on
  its own line.
- **Neither number counts what comes after the start.** That is watching latency, turning the
  kill switch off when the cheaper model slows, checking it is healthy before resuming,
  reading results and writing the report. The agent does those too (design.md section 6),
  but this file times setup only.
- **Neither counts one-time setup.** The two permanent features and the three metrics exist
  once per environment; both the agent and the dashboard pass find them on every later run.

## Difference

The dashboard pass minus Rollout Marshal's `marshal start` to `RUNNING`:
[[5.1: difference, about 2:18 minus marshal start to RUNNING, mm:ss]].
