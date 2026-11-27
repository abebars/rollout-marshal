# Demo video: script and shot list

Six beats, 2:57 in all: the ask, the plan and approval, the setup, the failure, the rollback
and resume, and the decision. The problem is stated in the first 15 seconds, and the video
ends on the two timed numbers.

## Recording notes

- **One real run, cut, not staged.** The video shows run `r202611272042` (task 5.2,
  `evidence/5.2/r202611272042.md`), the clean end-to-end run.
  - It took 22 min 37 s of real time, from `start` at 20:42:29Z to `HELD` at 21:05:06Z.
  - The video cuts between beats and keeps a UTC clock in shot (`scripts/e2e.py`'s
    timestamps, or the shell prompt's), so each cut shows how much time passed.
  - Nothing is re-enacted, and nothing is sped up without a caption saying so.
  - Where a screen was not captured while the run happened, show only what is left of it,
    captioned as a later read: `e2e.py`'s log, the three emails, the report, the completed
    experiment's page.
  - The "Complete experiment" dialog cannot be opened again once the experiment is ended,
    so show it, and the page's banner, only if they were captured at the time.
- **The numbers on the end card** are the ones in `docs/time-saved.md`:
  - the dashboard pass, about 2:18 with one metric attached. An AI agent clicked it through
    in Chrome at Ahmed's instruction, and the card says so: it was not a person;
  - Rollout Marshal's own work in run `r202611272042`, about 0:39 with all three metrics,
    with the 6:43 its plan waited for approval shown apart. The card also gives the first
    run's 1:20, which included a failed experiment start that the next tick retried.
- **The recommendation** on screen and in the voiceover is the one run `r202611272042`'s
  report states: `hold, re-run`. No run has recommended `promote`. Every planned run
  injects the fault, so by design each recommends `hold, re-run`, as both full runs did.
- **The dashboard's suggestion.** In run `r202611272042`, Flagsmith's experiment page read
  "80% Winner Probability Confirmed … cheaper is outperforming Control by +0.3% … Consider
  rolling out cheaper to all users". Its "Complete experiment" dialog pre-selected "Roll out
  a variant: cheaper" ("80% chance to win"). In the first run the page read 66%.
- **Never on screen:**
  - the incident email's address, wherever it shows: blur the To line and the mail client's
    account menu;
  - the signed-in identity in the AWS console's or the Flagsmith dashboard's account menu;
  - the unsubscribe link at the foot of each SNS email, which unsubscribes the address for
    whoever opens it;
  - the assistant API's URL, `ask_url`: the load reads it from a shell variable;
  - any Flagsmith key: keep the SDK Keys page and the organisation's API keys closed;
  - AWS credentials.
- **Screens:**
  - a terminal with a large font;
  - the Flagsmith dashboard, project "Toggle Wars", environment "Development";
  - the email client, with the incident address hidden;
  - the report, which the "results ready" email carries in full.
- **The voiceover** runs at about 150 words a minute. Each beat's lines fit inside its time.
- **The engineer** on screen is the Human. In run `r202611272042` he typed the launch, the
  resume and the decline. In the first run, Claude Code's auto mode had refused the lead
  agent's own attempt to approve. Rollout Marshal never ends an experiment: the lead agent
  ended experiment 1503 in the dashboard, under Ahmed's delegation.

## Overview

| # | Beat | Time | Main screen |
| --- | --- | --- | --- |
| 1 | The ask | 0:00 to 0:27 | terminal |
| 2 | The plan and approval | 0:27 to 0:57 | terminal, Flagsmith dashboard |
| 3 | The setup | 0:57 to 1:22 | terminal, Flagsmith dashboard |
| 4 | The failure | 1:22 to 1:52 | two terminals, the CloudWatch alarm |
| 5 | The rollback and resume | 1:52 to 2:22 | incident email, Flagsmith dashboard, terminal |
| 6 | The decision | 2:22 to 2:57 | the report, Flagsmith dashboard, terminal, end card |

## Beat 1: the ask (0:00 to 0:27)

| Time | On screen |
| --- | --- |
| 0:00 to 0:12 | Terminal at an empty prompt, the title "Rollout Marshal" in a corner. Caption: "Claude Sonnet 5's Bedrock list price per token is a fifth of Claude Fable 5.1's (us-east-1, `app/prices.json`)". |
| 0:12 to 0:27 | The engineer types `uv run marshal start "Test moving the support assistant from Claude Fable 5.1 to Claude Sonnet 5. Don't let answer quality or latency slip."` and presses Enter. |

Voiceover:

> Moving a production assistant to a cheaper model is a real cost decision that teams make
> by hand: set up flags, wire metrics, watch dashboards, decide in a meeting.
>
> Rollout Marshal does the whole loop from one prompt, and asks a person before every risky
> step.
>
> The ask: move our support assistant from Claude Fable 5.1 to Claude Sonnet 5, without
> letting quality or latency slip.

## Beat 2: the plan and approval (0:27 to 0:57)

| Time | On screen |
| --- | --- |
| 0:27 to 0:40 | Terminal: the reply. Run `r202611272042`, state `PLANNED`, the plan in its message (the hypothesis, primary metric `helpful_rate`, guardrails `mean_latency_ms` and `mean_cost_micro_usd`, the 50/50 split, the stop rules), and awaiting `launch`. |
| 0:40 to 0:47 | Flagsmith dashboard: the features list, searched for the run's ID. Nothing is there yet. |
| 0:47 to 0:57 | Terminal: the engineer types `uv run marshal approve r202611272042 launch`. Caption: "An approve names its step: launch, resume or promotion". |

Voiceover:

> In 6.6 seconds it returns a plan. The model wrote the hypothesis; the metrics, the split
> and the stop rules come from code.
>
> Flagsmith has nothing for this run yet: nothing changes until I approve. My approval names
> the step, launch, so it can't be mistaken for another.

## Beat 3: the setup (0:57 to 1:22)

| Time | On screen |
| --- | --- |
| 0:57 to 1:05 | Terminal: the approve's reply, state `RUNNING`, 32.7 s after the approve. |
| 1:05 to 1:13 | Flagsmith dashboard: feature `assistant_model_r202611272042`, value `current`, one variation `cheaper`. |
| 1:13 to 1:22 | Flagsmith dashboard: the experiment, running. Rollout 100 percent, split 50/50, metrics `helpful_rate`, `mean_latency_ms` and `mean_cost_micro_usd`, with `helpful_rate` shown as primary. |

Voiceover:

> On my approval it built the test in Flagsmith. The run's flag, its cheaper variation and
> the experiment went through Flagsmith's MCP server; the three metrics and the start, through
> the Admin API. It made sure the kill switch was on first. The experiment is live.

## Beat 4: the failure (1:22 to 1:52)

| Time | On screen |
| --- | --- |
| 1:22 to 1:32 | Terminal: `uv run python scripts/load.py --run-id r202611272042 --users 150 --questions 10 --url "$ASK_URL"`, running. |
| 1:32 to 1:40 | Second terminal: `scripts/inject_fault.sh` prints the FIS experiment's ID and its UTC start, 20:49:52Z, and end. |
| 1:40 to 1:52 | Cut, with the UTC clock in shot: the CloudWatch alarm `rm-cheaper-latency-p95` in ALARM, in the AWS console. It entered ALARM at 20:53:17Z, 3 min 24.5 s into the fault, and its invocation had claimed the rollback by 20:53:24Z. |

Voiceover:

> A hundred and fifty synthetic users start asking support questions, and Flagsmith splits
> them between the two models.
>
> Now I break the cheaper one on purpose. AWS Fault Injection Service adds ten seconds to
> every answer it gives, for six minutes.
>
> Three minutes and twenty-four seconds in, the p95 latency alarm on the cheaper arm fires.

## Beat 5: the rollback and resume (1:52 to 2:22)

| Time | On screen |
| --- | --- |
| 1:52 to 2:00 | Email client: the message "Rollout Marshal r202611272042: rolled back", with the incident note. The To line is blurred. |
| 2:00 to 2:05 | Flagsmith dashboard: `assistant_cheaper_enabled` is off. |
| 2:05 to 2:09 | Terminal: `uv run marshal status r202611272042` shows state `ROLLED_BACK`. |
| 2:09 to 2:15 | Cut, clock in shot: the email "Rollout Marshal r202611272042: ready to resume", 7 min 45 s after the rollback. The alarm had gone back to OK at 21:01:17Z. |
| 2:15 to 2:22 | Terminal: `uv run marshal approve r202611272042 resume`, then state `RUNNING`. |

Voiceover:

> Nobody asked it to. Four minutes twenty-two into the fault, the run is rolled back: the
> kill switch is off, so every user gets the current model, and the agent has sent me what
> it saw and did. That is the only change to traffic it makes without me.
>
> When the alarm cleared and five probes answered under its threshold, it asked to resume.
> I say yes.

## Beat 6: the decision (2:22 to 2:57)

| Time | On screen |
| --- | --- |
| 2:22 to 2:31 | Email client: "Rollout Marshal r202611272042: results ready". Scroll the report through Results, Guardrails, Incident and Known bias to the Recommendation: **`hold, re-run`**. Caption: "`helpful_rate` is near 1 in both arms: not evidence that the models are equally helpful". |
| 2:31 to 2:37 | Flagsmith dashboard: the experiment's results, the lift on `helpful_rate` with its 95 percent credible interval, and the page's banner: "80% Winner Probability Confirmed … Consider rolling out cheaper to all users". |
| 2:37 to 2:40 | Terminal: `uv run marshal decline r202611272042`, then state `HELD`. |
| 2:40 to 2:44 | Flagsmith dashboard: End Experiment. The "Complete experiment" dialog pre-selects "Roll out a variant: cheaper" ("80% chance to win"); "Roll out control" is chosen, and the page then reads "Control rolled out to everyone". |
| 2:44 to 2:57 | End card, three lines: "Flagsmith dashboard, clicked through in Chrome by an AI agent: about 2:18, one metric"; "Rollout Marshal's own work: about 0:39, all three metrics (run `r202611272042`; 1:20 in the first run, which retried a failed start)"; and "The plan also waited 6:43 for the engineer's approval". |

Voiceover:

> Flagsmith's dashboard gives cheaper an 80 percent win probability, and suggests rolling it
> out to everyone. The report says: hold, re-run, because the run had an incident.
>
> I hold it, and the experiment is ended in Flagsmith, never by Rollout Marshal.
>
> An AI agent clicking this setup through Flagsmith's dashboard took two minutes eighteen,
> with one metric. Rollout Marshal's own work took thirty-nine seconds, with all three,
> besides the nearly seven minutes its plan waited for my approval.

## Before recording

- Run `r202611272042`'s report is read, and its recommendation is the one in beat 6.
- The end card's numbers match `docs/time-saved.md`.
- Nothing from "Never on screen" appears, in any frame.
- The cut runs 3:00 or less.
