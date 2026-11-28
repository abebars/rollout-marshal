# Demo video: script and shot list

Six beats, 2:57 in all: the ask, the plan and approval, the setup, the failure, the rollback
and resume, and the decision. The problem is stated in the first 15 seconds, and the video
ends on the two timed numbers.

## Recording notes

- **One real run, recorded live, cut, not staged.** The video shows one new short run, 150
  users x 2 questions, screen-recorded as it happens. `docs/submission.md` ("Recording the
  run") gives its steps.
  - The two full runs, `r202611271927` and `r202611272042`, are the measured record
    (`evidence/5.1-e2e.md`, `evidence/5.2/r202611272042.md`). The recorded run is not one of
    them.
  - Each `[[rec: …]]` below is a value from the recorded run. Ahmed or the lead fills it from
    that run's `e2e.py` output and `evidence/6.4/`, before the cut. The table under "Filling
    the placeholders" says where each comes from.
  - The video cuts between beats and keeps a UTC clock in shot (`scripts/e2e.py`'s
    timestamps, or the shell prompt's), so each cut shows how much time passed.
  - Nothing is re-enacted, and nothing is sped up without a caption saying so.
  - If an effect fails and the next tick finishes it, as two of the four Flagsmith-heavy
    effects did in the full runs, the video shows that as it happened, with a caption.
  - Where a screen was not captured while the run happened, show only what is left of it,
    captioned as a later read: `e2e.py`'s log, the three emails, the report, the completed
    experiment's page.
  - The "Complete experiment" dialog cannot be opened again once the experiment is ended,
    so show it, and the page's banner, only if they were captured at the time.
- **The numbers on the end card** are the two full runs', from `docs/time-saved.md`. The
  recorded run does not change them:
  - the dashboard pass, about 2:18 with one metric attached. An AI agent clicked it through
    in Chrome at Ahmed's instruction, and the card says so: it was not a person;
  - Rollout Marshal's own work, about 0:39 to 1:20 with all three metrics: 0:39 in
    `r202611272042`, and 1:20 in `r202611271927`, which included a failed experiment start
    that the next tick retried. The plan's wait for the engineer's approval, 6:43 and 12:33,
    is shown apart.
- **The recommendation** on screen and in the voiceover is the one the recorded run's report
  states. No run has recommended `promote`. Every planned run injects the fault, so by design
  each recommends `hold, re-run`, as both full runs did. The video claims no promote.
- **The dashboard's suggestion.** In both full runs, Flagsmith's experiment page favoured
  cheaper (66 and 80 percent winner probability) on a sample the rollback skewed, and its
  "Complete experiment" dialog pre-selected "Roll out a variant: cheaper". Beat 6 shows the
  recorded run's own banner and pre-selection.
- **Who ends the experiment.** Rollout Marshal never ends one. In the recorded run it is
  ended in the dashboard, on camera (`docs/submission.md`, step 4). In the two full runs the
  lead agent clicked it, under Ahmed's delegation.
- **Never on screen:**
  - the incident email's address, wherever it shows: blur the To line and the mail client's
    account menu;
  - the signed-in identity in the AWS console's or the Flagsmith dashboard's account menu;
  - the unsubscribe link at the foot of each SNS email, which unsubscribes the address for
    whoever opens it;
  - the assistant API's URL, `ask_url`: `e2e.py` finds it itself and prints `<not shown>`
    in its place;
  - any Flagsmith key: keep the SDK Keys page and the organisation's API keys closed;
  - AWS credentials.
- **Screens:**
  - two terminals with a large font: the recorded one, where `e2e.py` runs, and a second for
    the engineer's commands;
  - the Flagsmith dashboard, project "Toggle Wars", environment "Development";
  - the AWS console, for the alarm;
  - the email client, with the incident address hidden;
  - the report, which the "results ready" email carries in full.
- **The voiceover** runs at about 150 words a minute. Each beat's lines fit inside its time.
- **The engineer** on screen is the Human. He types the launch, the resume and the decline in
  the second terminal, each as `e2e.py` prints it (`docs/submission.md`, step 3). `e2e.py`
  approves nothing itself. Claude Code's auto mode refused the lead agent's one attempt to
  approve, on `r202611271018`, a start that never launched (`evidence/5.1-e2e.md`).

## Overview

| # | Beat | Time | Main screen |
| --- | --- | --- | --- |
| 1 | The ask | 0:00 to 0:27 | terminal |
| 2 | The plan and approval | 0:27 to 0:57 | both terminals, Flagsmith dashboard |
| 3 | The setup | 0:57 to 1:22 | terminal, Flagsmith dashboard |
| 4 | The failure | 1:22 to 1:52 | terminal, the CloudWatch alarm |
| 5 | The rollback and resume | 1:52 to 2:22 | incident email, Flagsmith dashboard, both terminals |
| 6 | The decision | 2:22 to 2:57 | the report, Flagsmith dashboard, both terminals, end card |

## Beat 1: the ask (0:00 to 0:27)

| Time | On screen |
| --- | --- |
| 0:00 to 0:12 | Terminal at an empty prompt, the title "Rollout Marshal" in a corner. Caption: "Claude Sonnet 5's Bedrock list price per token is a fifth of Claude Fable 5.1's (us-east-1, `app/prices.json`)". |
| 0:12 to 0:27 | The engineer enters the command line from `docs/submission.md`, step 2, which carries the full prompt: `uv run python scripts/e2e.py --pause-minutes 180 --questions 2 --prompt "Test moving the support assistant from Claude Fable 5.1 to Claude Sonnet 5. Don't let answer quality or latency slip." --record evidence/6.4/record.json`. `e2e.py` prints its checks, then `start sent: marshal start "…"`. Caption: "`scripts/e2e.py` sends the start and reads the run's status every 10 s. It approves nothing". |

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
| 0:27 to 0:40 | Terminal: the plan `e2e.py` printed. Run `[[rec: run ID]]`, state `PLANNED`, the plan in its message (the hypothesis, primary metric `helpful_rate`, guardrails `mean_latency_ms` and `mean_cost_micro_usd`, the 50/50 split, the stop rules), and awaiting `launch`. Then `e2e.py`'s pause, with the command to type. |
| 0:40 to 0:47 | Flagsmith dashboard: the features list, searched for the run's ID. Nothing is there yet. |
| 0:47 to 0:57 | Second terminal: the engineer types `uv run marshal approve [[rec: run ID]] launch`. Caption: "An approve names its step: launch, resume or promotion". |

Voiceover:

> In [[rec: plan, seconds after the start]] seconds it returns a plan. The model wrote the
> hypothesis; the metrics, the split and the stop rules come from code.
>
> Flagsmith has nothing for this run yet: nothing changes until I approve. My approval names
> the step, launch, so it can't be mistaken for another.

## Beat 3: the setup (0:57 to 1:22)

| Time | On screen |
| --- | --- |
| 0:57 to 1:05 | Terminal: `e2e.py`'s lines `approve seen: …`, then `RUNNING: run [[rec: run ID]] is running`, [[rec: RUNNING, seconds after the approve was seen]] s later. |
| 1:05 to 1:13 | Flagsmith dashboard: feature `assistant_model_[[rec: run ID]]`, value `current`, one variation `cheaper`. |
| 1:13 to 1:22 | Flagsmith dashboard: the experiment, running. Rollout 100 percent, split 50/50, metrics `helpful_rate`, `mean_latency_ms` and `mean_cost_micro_usd`, with `helpful_rate` shown as primary. |

Voiceover:

> On my approval it built the test in Flagsmith. The run's flag, its cheaper variation and
> the experiment went through Flagsmith's MCP server; the three metrics and the start, through
> the Admin API. It made sure the kill switch was on first. The experiment is live.

## Beat 4: the failure (1:22 to 1:52)

| Time | On screen |
| --- | --- |
| 1:22 to 1:32 | Terminal, with nothing typed: `e2e.py`'s own lines as it starts the load and the fault, `started scripts/load.py --run-id [[rec: run ID]] --users 150 --questions 2 --url <not shown> (pid …)` and `started scripts/inject_fault.sh (pid …)`, then the load's first line. |
| 1:32 to 1:40 | Same terminal: `[fault] inject_fault: experiment … started at [[rec: fault start, UTC]]`, then its `state running` lines. |
| 1:40 to 1:52 | Cut, with the UTC clock in shot: the CloudWatch alarm `rm-cheaper-latency-p95` in ALARM, in the AWS console. It entered ALARM at [[rec: ALARM, UTC]], [[rec: ALARM, after the fault started]] into the fault. Then `e2e.py`'s next status line, with the rollback claimed (`RUNNING -> ROLLED_BACK pending`) or done (`ROLLED_BACK`). |

Voiceover:

> A hundred and fifty synthetic users start asking support questions, and Flagsmith splits
> them between the two models.
>
> Then the driver breaks the cheaper one on purpose. AWS Fault Injection Service adds ten
> seconds to every answer it gives, for six minutes.
>
> [[rec: ALARM, after the fault started, in words]] in, the p95 latency alarm on the cheaper
> arm fires.

## Beat 5: the rollback and resume (1:52 to 2:22)

| Time | On screen |
| --- | --- |
| 1:52 to 2:00 | Email client: the message "Rollout Marshal [[rec: run ID]]: rolled back", with the incident note. The To line is blurred. |
| 2:00 to 2:05 | Flagsmith dashboard: `assistant_cheaper_enabled` is off. |
| 2:05 to 2:09 | Terminal: `e2e.py`'s line `status: ROLLED_BACK`. |
| 2:09 to 2:15 | Cut, clock in shot: the email "Rollout Marshal [[rec: run ID]]: ready to resume", [[rec: resume request, after the rollback]] after the rollback. The alarm had gone back to OK at [[rec: OK, UTC]]. |
| 2:15 to 2:22 | Second terminal: `uv run marshal approve [[rec: run ID]] resume`. Then `e2e.py`'s line `RUNNING again: run [[rec: run ID]] is resumed`. |

Voiceover:

> Nobody asked it to. [[rec: rollback, after the fault started, in words]] into the fault, the
> run is rolled back: the kill switch is off, so every user gets the current model, and the
> agent has sent me what it saw and did. That is the only change to traffic it makes without
> me.
>
> When the alarm cleared and five probes answered under its threshold, it asked to resume.
> I say yes.

## Beat 6: the decision (2:22 to 2:57)

| Time | On screen |
| --- | --- |
| 2:22 to 2:31 | Email client: "Rollout Marshal [[rec: run ID]]: results ready". Scroll the report through Results, Guardrails, Incident and Known bias to the Recommendation. The scroll ends on the bold **`hold, re-run`** and the rule paragraph under it, which code wrote. The model-written paragraph after them stays out of shot: in both full runs' reports it understated the failed latency guardrail. If `helpful_rate` is near 1 in both arms, caption: "`helpful_rate` is near 1 in both arms: not evidence that the models are equally helpful". |
| 2:31 to 2:37 | Flagsmith dashboard: the experiment's results, the lift on `helpful_rate` with its 95 percent credible interval, and the page's banner: [[rec: the banner's text]]. Caption: "The rollback skewed this sample: cheaper-arm users got the current model while the kill switch was off". |
| 2:37 to 2:40 | Second terminal: `uv run marshal decline [[rec: run ID]]`. Then `e2e.py`'s line `HELD: Run [[rec: run ID]] is held`. |
| 2:40 to 2:44 | Flagsmith dashboard, on camera: End Experiment. The "Complete experiment" dialog pre-selects [[rec: the dialog's pre-selection]]; "Roll out control" is chosen, and the page then reads "Control rolled out to everyone". |
| 2:44 to 2:57 | End card, three lines: "Flagsmith dashboard, clicked through in Chrome by an AI agent: about 2:18, one metric"; "Rollout Marshal's own work: about 0:39 to 1:20, all three metrics"; and "From the two full runs, whose plans also waited 6:43 and 12:33 for the engineer's approval". |

Voiceover:

> Flagsmith's dashboard says [[rec: the banner, in words]]. The report says: hold, re-run,
> because the run had an incident.
>
> I hold it, and the experiment is ended in Flagsmith, never by Rollout Marshal.
>
> In our two full runs, an AI agent clicking this setup through Flagsmith's dashboard took
> about two minutes eighteen, with one metric. Rollout Marshal's own work took thirty-nine
> seconds to a minute twenty, with all three, besides the time its plan waited for my
> approval.

## Filling the placeholders

Each comes from the recorded run. The run's record in `evidence/6.4/` holds its history, its
four timestamps, the report key and the SNS IDs (`docs/submission.md`, step 6).

| Placeholder | From |
| --- | --- |
| `[[rec: run ID]]` | `e2e.py`'s `run:` line |
| `[[rec: plan, seconds after the start]]` | `e2e.py`'s `start sent` and `plan returned` timestamps |
| `[[rec: RUNNING, seconds after the approve was seen]]` | `e2e.py`'s `approve seen` and `RUNNING` timestamps |
| `[[rec: fault start, UTC]]` | `inject_fault.sh`'s `started at` line |
| `[[rec: ALARM, UTC]]`, `[[rec: ALARM, after the fault started]]` and its words | the alarm's history in `evidence/6.4/`, against FIS's start, which `inject_fault.sh`'s last line gives |
| `[[rec: rollback, after the fault started, in words]]` | the run's `ROLLED_BACK` entry, against FIS's start |
| `[[rec: OK, UTC]]` | the alarm's history |
| `[[rec: resume request, after the rollback]]` | the run's `AWAITING_RESUME` and `ROLLED_BACK` entries |
| `[[rec: the banner's text]]`, `[[rec: the banner, in words]]` | the experiment page, as recorded |
| `[[rec: the dialog's pre-selection]]` | the "Complete experiment" dialog, as recorded |

## Before recording

- `scripts/reset.py` ended with "no experiment is running" (`docs/submission.md`, step 1).
- `evidence/6.4/` exists and holds no `record.json`: `--record` needs its folder, and refuses
  an existing file (step 2).
- The screens above are open, and nothing from "Never on screen" is.

## Before the cut

- Every `[[rec: …]]` is filled.
- The recorded run's report is read, and its recommendation is the one in beat 6. If it is
  not `hold, re-run`, beat 6 changes to match.
- The end card's numbers match `docs/time-saved.md`.
- Nothing from "Never on screen" appears, in any frame.
- The cut runs 3:00 or less.
