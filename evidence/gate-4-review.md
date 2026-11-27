# Gates 3 and 4 review

docs/REVIEW.md puts Gate 3 after task 5.1 and Gate 4 after 5.2, 6.1, 6.2 and 6.3. 5.2 ran before Gate 3's review, so both gates were reviewed together, once, on a preview of the final `main`. Each gate kept its own two hats and its own verdict.

| | |
| --- | --- |
| Head reviewed | `lead/final-preview` at b2e5d3f: `dev` at ec9fa92, plus PR #41 (6.1 to 6.3: README, time saved, video script) and PR #42 (6.4: the submission checklist) |
| Reviewers | Five goldfish, run in parallel on 2026-11-27 from about 21:41Z to 22:27Z. Gate 3: the builder and the sceptical judge. Gate 4: the builder, the sceptical judge, and a reader given only the README (6.2's Pass). A verdict step per gate then reproduced each finding before keeping it |
| Verdicts | Gate 3: `changes`, 2 blockers and 12 nits. Gate 4: `changes`, 2 blockers and 16 nits. Both gates raised the same two blockers |
| What held | Every fact the runs rest on reproduced as `rm-review-readonly`: both run items, both S3 reports, the alarm's history, the SNS publishes, the cost sums and the runtime versions |
| Human | Ahmed, in chat, 2026-11-27 (below) |

## Ahmed's approval and decision
- **21:42:17Z:** "Great it's all approved. Make sure that the readme for the project is polished, nice, and working as expected." That covers Gates 3 and 4, the video script (6.3's Pass), and the merge of `dev` to `main`, after the fixes below. A second message, at 21:42:25Z, asked that the README keep the architecture diagrams.
- **22:28:26Z:** both gates' second blocker needed a choice, so the lead asked which run the video should show. Ahmed answered "Record a new short run (Recommended)": one new run at 150 x 2, screen-recorded as it happens.

## The Pass of each task

| Task | Pass | How |
| --- | --- | --- |
| 5.1 | Met | The run item's seven entries and the four `e2e.py` timestamps reproduced. So did 1,500 answers sent, the report key and three SNS IDs. Ahmed's approval accepts it as one of the two full runs, although its driver stopped at the probe bug (`evidence/5.1-e2e.md`) |
| 5.2 | Met | `e2e.py` drove it from start to HELD and exited 0. No code changed between the runs: 0 diff lines in `e2e.py`, no Lambda update after 10:17:21Z, and runtime version 5 throughout |
| 6.1 | Met | Both numbers, the step list and the method are in `docs/time-saved.md` |
| 6.2 | Met | From the README alone, the reader said correctly what the project does. It named all four simulated parts (synthetic users, the injected fault, model-scored helpfulness and the rollback bias) and both known biases |
| 6.3 | Met | Ahmed approved it at 21:42:17Z, and it is reworked below for the new short run |

## Findings and what was done
G3 items are Gate 3's, B and N items Gate 4's. Where both gates raised the same thing, it has one row.

| Item | Finding | Done |
| --- | --- | --- |
| G3-1, B2 | The README contradicted itself on who ends experiments: its safety lines said the engineer always does, while the lead agent ended 1495 and 1503 | README: Rollout Marshal never ends an experiment; in this build the lead agent clicked it, under Ahmed's delegation |
| G3-2, B1 | The video script said it filmed `r202611272042` live, which was never recorded, while `docs/submission.md` planned a new run | Ahmed chose a new short run. The script now names the run recorded per `docs/submission.md`, with placeholders for its numbers, and shows `e2e.py`'s own lines where it starts the load and the fault. `docs/submission.md` gives the exact steps |
| G3-3, N15 | 5.1's apply ran without `make build`, so the app functions still run zips from before c7. The deployed functions carry an older `names.py`: comments and constants only tests read | Recorded in `evidence/5.1-e2e.md`. Behaviour is unchanged. Optionally, after the merge, a `make build`, plan and apply from `main`, with Ahmed's yes |
| G3-4 | `make test` skips the two tests that run the shipped OpenTelemetry instrumentation, the code that broke 5.1's probe | New `make test-runtime` target (6 passed), named in `make help` and the README |
| G3-5 | The FIS extension refuses the 10 s slow-poll setting and polls every 60 s, so design.md section 9's "so the fault starts quickly" is wrong | Recorded as 5.1's finding 6. Open: the contract line is corrected after judging |
| G3-6, G3-7 | 5.1's launch times were mislabelled, two lines were out of order, and the reads behind the launch's invoke times were missing | Fixed in `evidence/5.1-e2e.md`, with rows 30a and 30b and the wrapper note |
| G3-8, N2 | `docs/time-saved.md` tied the launch's timeout to a cold start | Reworded: the warm 4.0 s invoke failed, and the cold one before it succeeded |
| G3-9 | `evidence/gate-2-review.md` still said the lead runs the approvals | Dated correction there: Ahmed typed every approve and decline |
| G3-10, N8 | Whether 5.1 counts as a full run | Recorded in `evidence/5.1-e2e.md`. The Definition of done reads "Two full runs ending HELD" |
| G3-11, N1 | The agent's launch time is split at `e2e.py`'s 10 s polls | One line in `docs/time-saved.md` and the README. By the runtime's log the launches took 37.9 s and 77.5 s |
| G3-12, N4 | The dashboard banners read samples the rollback skewed | `evidence/5.1-e2e.md` and the README: Sonnet 5 served 153 of 740 and 256 of 690 cheaper-arm answers, and the readings are the lead's |
| G3-13 | Delivery of the incident notes was unverified | SNS delivered each note (1 delivered, 0 failed each time); inbox arrival not checked |
| G3-14 | 5.1's report was not in the repo | Attached as `evidence/5.1/report-r202611271927.md`; the README points to both attachments |
| N3, N5, N6, N7, N14, N16 | README and video-script wording: the auto-mode refusal was on `r202611271018`; the report's model-written prose; no run has been promoted; what the second run found; the state bucket and preflight's gh line; five points a newcomer could not settle | Fixed in the README and the video script |
| N9 | Gate 3 left no record | This file |
| N10, N11, N12, N13 | Cleanup gaps (SSM `/rm/agent-runtime-arn`, empty the reports bucket before destroy), 807 and 819 missing from the list, the checklist's order, and the recorded run's after-load reads and evidence | Fixed in `docs/submission.md` |

## Still open
1. The recording, the upload and the WeMakeDevs entry: Ahmed's (`docs/submission.md`).
2. Ahmed's written confirmation of the quoted words in `evidence/gate-2-review.md`, with one comment on PR #36.
3. CLAUDE.md's rule on who ends experiments, which needs Ahmed's decision.
4. Two Flagsmith timeouts inside effects, each recovered by a tick: 5.1's finding 2 and 5.2's finding 1. Owner A; no cause settled.
5. design.md section 9's slow-poll line (5.1's finding 6).
6. The deployed functions' older `names.py`. Optional re-apply after the merge.
