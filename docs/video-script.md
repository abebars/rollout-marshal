<!-- Draft (task 6.3). Each [[source: what]] is filled from the source it names before Gate 4.
[[run: what]] comes from the evidence of the run being recorded: evidence/5.1-e2e.md, or that
run's history under evidence/5.2/. Delete this comment once the last one is filled. -->

# Demo video: script and shot list

Six beats, 2:55 in all: the ask, the plan and approval, the setup, the failure, the rollback
and resume, and the decision. The problem is stated in the first 15 seconds, and the video
ends on the two timed numbers.

## Recording notes

- **One real run, cut, not staged.** Record one planned run as it happens: [[run: run ID]].
  It takes [[run: start to final state, minutes]] minutes of real time. The video cuts
  between beats and keeps a UTC clock in shot (`scripts/e2e.py`'s timestamps, or the shell
  prompt's), so each cut shows how much time passed. Nothing is re-enacted, and nothing is
  sped up without a caption saying so.
- **The numbers on the end card** are the two in `docs/time-saved.md`. The first is the
  dashboard pass, which an AI agent clicked through in Chrome at Ahmed's instruction, and the
  card says so: it was not a person. The second is `marshal start` to `RUNNING` in run
  [[5.1: run ID]]; if the recorded run is a different one, the card says it comes from 5.1's
  run.
- **The recommendation** on screen and in the voiceover is the one the recorded run's report
  states. Every planned run injects the fault, so by design it is `hold, re-run`. The video
  says `promote` only if that report did.
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
- **The engineer** on screen is the Human, who runs every `marshal approve` and
  `marshal decline` and ends the experiment in the dashboard.

## Overview

| # | Beat | Time | Main screen |
| --- | --- | --- | --- |
| 1 | The ask | 0:00 to 0:27 | terminal |
| 2 | The plan and approval | 0:27 to 0:57 | terminal, Flagsmith dashboard |
| 3 | The setup | 0:57 to 1:22 | terminal, Flagsmith dashboard |
| 4 | The failure | 1:22 to 1:52 | two terminals, the CloudWatch alarm |
| 5 | The rollback and resume | 1:52 to 2:22 | incident email, Flagsmith dashboard, terminal |
| 6 | The decision | 2:22 to 2:55 | the report, Flagsmith dashboard, terminal, end card |

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
| 0:27 to 0:40 | Terminal: the reply. Run [[run: run ID]], state `PLANNED`, the plan in its message (the hypothesis, primary metric `helpful_rate`, guardrails `mean_latency_ms` and `mean_cost_micro_usd`, the 50/50 split, the stop rules), and awaiting `launch`. |
| 0:40 to 0:47 | Flagsmith dashboard: the features list, searched for the run's ID. Nothing is there yet. |
| 0:47 to 0:57 | Terminal: the engineer types `uv run marshal approve [[run: run ID]] launch`. Caption: "An approve names its step: launch, resume or promotion". |

Voiceover:

> In [[run: start sent to plan returned, seconds]] seconds it returns a plan. The model
> wrote the hypothesis; the metrics, the split and the stop rules come from code.
>
> Flagsmith has nothing for this run yet: nothing changes until I approve. My approval names
> the step, launch, so it can't be mistaken for another.

## Beat 3: the setup (0:57 to 1:22)

| Time | On screen |
| --- | --- |
| 0:57 to 1:05 | Terminal: the approve's reply, state `RUNNING`. |
| 1:05 to 1:13 | Flagsmith dashboard: feature `assistant_model_<run_id>` for [[run: run ID]], value `current`, one variation `cheaper`. |
| 1:13 to 1:22 | Flagsmith dashboard: the experiment, running. Rollout 100 percent, split 50/50, metrics `helpful_rate`, `mean_latency_ms` and `mean_cost_micro_usd`, with [[5.1: metric the dashboard shows as primary]] shown as primary. |

Voiceover:

> On my approval it built the test in Flagsmith. The run's flag, its cheaper variation and
> the experiment went through Flagsmith's MCP server; the three metrics and the start, through
> the Admin API. It made sure the kill switch was on first. The experiment is live.

## Beat 4: the failure (1:22 to 1:52)

| Time | On screen |
| --- | --- |
| 1:22 to 1:32 | Terminal: `uv run python scripts/load.py --run-id [[run: run ID]] --users 150 --questions 10 --url "$ASK_URL"`, running. |
| 1:32 to 1:40 | Second terminal: `scripts/inject_fault.sh` prints the FIS experiment's ID and its UTC start and end. |
| 1:40 to 1:52 | Cut, with the UTC clock in shot, [[run: fault start to ALARM, minutes]] minutes later: the CloudWatch alarm `rm-cheaper-latency-p95` in ALARM, in the AWS console. |

Voiceover:

> A hundred and fifty synthetic users start asking support questions, and Flagsmith splits
> them between the two models.
>
> Now I break the cheaper one on purpose. AWS Fault Injection Service adds ten seconds to
> every answer it gives, for six minutes.
>
> [[run: fault start to ALARM, minutes]] minutes in, the p95 latency alarm on the cheaper arm
> fires.

## Beat 5: the rollback and resume (1:52 to 2:22)

| Time | On screen |
| --- | --- |
| 1:52 to 2:00 | Email client: the message "Rollout Marshal [[run: run ID]]: rolled back", with the incident note. The To line is blurred. |
| 2:00 to 2:05 | Flagsmith dashboard: `assistant_cheaper_enabled` is off. |
| 2:05 to 2:09 | Terminal: `uv run marshal status [[run: run ID]]` shows state `ROLLED_BACK`. |
| 2:09 to 2:15 | Cut, clock in shot: the email "Rollout Marshal [[run: run ID]]: ready to resume". |
| 2:15 to 2:22 | Terminal: `uv run marshal approve [[run: run ID]] resume`, then state `RUNNING`. |

Voiceover:

> Nobody asked it to. Within [[run: ALARM to ROLLED_BACK, seconds]] seconds of the alarm,
> the agent turned the kill switch off, so every user is back on the current model, and
> emailed me what it saw and did. That is the only change to traffic it makes without me.
>
> When the alarm cleared and five probe calls all answered under its threshold, it asked to
> resume. I say yes.

## Beat 6: the decision (2:22 to 2:55)

| Time | On screen |
| --- | --- |
| 2:22 to 2:32 | Email client: "Rollout Marshal [[run: run ID]]: results ready". Scroll the report through Results, Guardrails, Incident and Known bias to the Recommendation: **[[run: recommendation, as the report states it]]**. |
| 2:32 to 2:38 | Flagsmith dashboard: the experiment's results, the lift on `helpful_rate` with its 95 percent credible interval. |
| 2:38 to 2:42 | Flagsmith dashboard: the engineer clicks End Experiment and chooses [[run: rollout decision chosen when the experiment was ended]]. |
| 2:42 to 2:45 | Terminal: [[run: the decision, either `uv run marshal decline <run_id>` then state `HELD`, or `uv run marshal approve <run_id> promotion` then state `PROMOTED`]]. |
| 2:45 to 2:55 | End card, two lines: "Flagsmith dashboard, clicked through in Chrome by an AI agent: about 2:18, primary metric only" and "Rollout Marshal, prompt to running: [[5.1: marshal start to RUNNING, mm:ss]], [[5.1: review, plan returned to approve seen, mm:ss]] of it the engineer reading the plan". |

Voiceover:

> Results are in. The report has Flagsmith's numbers, the guardrails, the incident, and the
> bias the rollback left: rolled-back users still count in the cheaper arm. So the fixed
> rules say: [[run: recommendation, as the report states it]].
>
> I end the experiment in Flagsmith myself, and [[run: the decision, in words]].
>
> An AI agent clicking this setup through Flagsmith's dashboard took about two minutes
> eighteen, with only the primary metric attached. Rollout Marshal took
> [[5.1: marshal start to RUNNING, mm:ss]], my review included.

## Before recording

- The recorded run's report is read, and its recommendation is the one in beat 6.
- Every placeholder is filled, and the end card's numbers match `docs/time-saved.md`.
- Nothing from "Never on screen" appears, in any frame.
- The cut runs 3:00 or less.
