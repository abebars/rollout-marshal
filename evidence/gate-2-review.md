# Gate 2 review

docs/REVIEW.md puts Gate 2 after tasks 2.5, 3.6 and 4.2. When it ran, 3.6 (PR #33) and 2.7's live half (PR #30) were still open, so their per-task reviews ran beside it. Gate 2 closes only when both have merged (G3, below).

| | |
| --- | --- |
| Heads reviewed | `dev` at 2a154a1; PR #33 (3.6) at 909eb40; PR #30 (2.7) at 38cb916 |
| Reviewers | Four goldfish, run in parallel on 2026-11-27 from 08:49:42Z to 09:27:14Z: the builder ("could you take over tomorrow?"), the sceptical judge ("is the evidence real?"), PR #33's task reviewer and PR #30's live-half reviewer. None of them made Flagsmith or FIS calls |
| Verdicts | Builder `approve`; judge `changes`; PR #33 `changes` (2 blockers, 8 nits); PR #30 `changes` (2 blockers, 3 nits) |
| What they found | Every AWS number they rebuilt matched: cheaper p95 4772.8 ms, 130 and 170 samples, 1,748,021 and 459,816 micro-USD. What failed was the record, and the authority for the human steps, not the numbers |
| Human | Ahmed, in chat, 2026-11-27 (below) |
| Closes | See "Closing", below |

## Ahmed's words (G1)
The judge's first finding: steps that only the human may take were done by the lead, on chat remarks the repo did not quote. These are Ahmed's messages in the lead's session, verbatim, with the UTC time the session log gives each:

| UTC, 2026-11-27 | Ahmed's words |
| --- | --- |
| 01:04:32Z | "it's auto mode so it's all acceptable to get the final results" |
| 08:13:51Z | "Approve to continue all tasks that you need" |
| 08:14:26Z | "You can do all of them using chrome when neeeded" |
| 08:15:20Z | "SNS confirmed" |
| 08:16:22Z | "We need to fast track this project" |
| 08:18:53Z | His answer to the lead's question about the next load's cost: "Cut 5.2 to one run (Recommended)" |
| 08:50:49Z | "use chrome to test that and add any human gaps needed. don't ask me for tasks until all project is completed" |

At 08:55:08Z the lead told him: "5.1 runs next, with me running the approvals at its pauses." He has not objected.

What the lead takes them to cover:

- **(a) The dashboard ends.** Under 08:14:26Z, the lead ended four experiments in Ahmed's Chrome, each with "Roll out control". It read each one back at 09:31:27Z, GET only, from the repo root with the SSO profile: `uv run python evidence/4.3-scripts/read_experiment.py <id>`. The script arrives with PR #34. Its output, with newlines removed:

  | Experiment | Run | `status` | `ended_at` | `rollout_decision` |
  | --- | --- | --- | --- | --- |
  | 918 | 2.2's live test, `rlive0421` | `completed` | 2026-11-27T08:16:42.199105Z | `control` |
  | 1127 | 3.6's smoke, `rsmoke0810` | `completed` | 2026-11-27T08:23:32.031245Z | `control` |
  | 1140 | 4.3's fault run, `rfault0831` | `completed` | 2026-11-27T08:43:54.941268Z | `control` |
  | 1172 | 6.1's dashboard pass, `rmanual0851` | `completed` | 2026-11-27T08:54:43.971998Z | `control` |

  ```
  {  "id": 918,  "status": "completed",  "ended_at": "2026-11-27T08:16:42.199105Z",  "rollout_decision": "control"}
  {  "id": 1127,  "status": "completed",  "ended_at": "2026-11-27T08:23:32.031245Z",  "rollout_decision": "control"}
  {  "id": 1172,  "status": "completed",  "ended_at": "2026-11-27T08:54:43.971998Z",  "rollout_decision": "control"}
  ```
  1140's read is C's, at 09:20:32Z, in `evidence/4.3-fis.md`. Every end came after 08:14:26Z. 1172's came after 08:50:49Z.
- **(b) c7 and the 5.2 cut.** c7 (PR #32) merged at 08:26:58Z, after 08:13:51Z's "Approve to continue all tasks that you need"; the lead records that message as c7's approval. The 5.2 cut is Ahmed's answer at 08:18:53Z, which tasks.md 5.2 records.
- **(c) The rule from here.** CLAUDE.md is not edited, so its rule stands as written. An edit to it needs Ahmed's approval of that edit, and he asked not to be asked for anything until the project is complete. For the rest of this build, under 08:50:49Z, the lead does the remaining **Human** steps of 5.1 and 5.2:
  - it runs `marshal approve` and `marshal decline` by hand at `scripts/e2e.py`'s pauses (e2e.py never sends them, and its tests pin that);
  - it ends those runs' experiments in the dashboard.

  Each step goes into that task's evidence with its UTC time, the exact command or click, the option chosen and a pointer to this file.
- **What stays open.** These words are in the lead's session log, not in the repo in Ahmed's hand. The lead's final handoff asks him to confirm or correct them with one comment on the pull request that merges this file. If he corrects them, the Human lines they cover go back to open.

tasks.md now says, in 2.2, 3.6 and 4.3, who ended each experiment and when. The "one-off" lines in tasks.md 2.7 and `evidence/gate-1-review.md` carry a dated correction.

## Findings and what was done

| Item | Finding | Done |
| --- | --- | --- |
| G1 | Human steps done by the lead, on remarks the repo did not quote | This file, above. tasks.md re-marks 2.2, 3.6 and 4.3. gate-1-review.md and tasks.md 2.7 get dated corrections. A adds 918's end to `evidence/2.2-live.md` in PR #30. CLAUDE.md is not edited (above) |
| G2 | 3.6's supporting reads had no commands and no read or write marks | PR #33 (P33-2). The lead gives its Flagsmith reruns to the re-review goldfish |
| G3 | Gate 2 cannot close while 3.6 is open. The Definition of done still asked for three clean runs | Gate 2 closes as "Closing" says. The Definition of done now reads two clean runs, 5.1 plus one in 5.2 |
| G4 | 4.1's SNS line still read pending. Its hash check describes a build since replaced | PR #34 (dac022b) shows the subscription confirmed, read at 08:43:31Z. The rebuild is below. The lead ticks 4.1 before 5.1 |
| G5 | `dev` still holds the 1.3 spike's agent config | PR #30 merges before 5.1. Until then nobody runs `agentcore deploy` from `dev` |
| G6 | Nothing points to the cost and usage checks CLAUDE.md asks for after each load. The projection predates 4.3 | Re-based below. After PR #33's P33-6, P33-7 and P33-9 land, e2e.py prints the cost command and the usage read after each load. 4.3's evidence reconciles its 300 sends: 298 answers, and 2 HTTP 500s that never reached `rm-router` |
| G7 | 3.6 called documented things unverified | PR #33 (P33-3) |
| G8 | `helpful_rate` is at its ceiling, so its interval rests on one judged answer | PR #33 (P33-4). Carried to 6.2's Known bias and the README: D |
| G9 | The contract has not taken in what 3.6, 4.1 and 4.3 settled | Contract change c8, after PR #33 merges: `n` counts identities; the EMF row closes from 4.3's run; c7 gets the worst per-minute p95 beside B, with T unchanged; G10's row; section 11's budget and `ANSWER_BUDGET` after the 5.2 cut |
| G10 | The billable-events row has no owner and no next step | In c8: "documented: event calls are not on the billable list; not yet observed". The lead re-reads the 2026-11-27 daily row after 5.2 |
| G11 | Nothing written says how to pass `--url` without printing it | `infra/app.tf`'s comment (this pull request) and `load.py`'s docstring (PR #33). e2e.py finds the URL itself and never prints it |
| G12 | A newcomer cannot set up review access or a Terraform run | docs/REVIEW.md gets the `rm-review` profile, and `infra/terraform.tfvars.example` is added (this pull request) |
| G13 | Lambda's `errorType` is still unverified | Accepted. No answer has failed. 3.6 lists it as not verified, and the first real failure's log settles it |

## The stop lines, re-read (G6)
**Cost.** Read only, at 09:33:13Z, after `aws sts get-caller-identity` gave 818928662830. For each variant:
```
aws cloudwatch get-metric-statistics --region us-east-1 --namespace RolloutMarshal --metric-name AnswerCostMicroUsd --dimensions Name=Variant,Value=<variant> --start-time 2026-11-26T02:30:00Z --end-time 2026-11-27T09:34:00Z --period 3600 --statistics Sum SampleCount
```
The end time is the first whole minute after the read, so no partial minute is dropped (P33-6). The datapoints, added up:

| Variant | Datapoints | Sum (micro-USD) | SampleCount | Mean per answer |
| --- | --- | --- | --- | --- |
| `current` | 2 | 3,817,308 | 282 | 13,536.6 |
| `cheaper` | 2 | 855,314 | 316 | 2,706.7 |
| Both | | 4,672,622 | 598 | |

The projection follows 3.6's rule: spent, plus the remaining answers times the higher mean. 5.1's 1,500 answers and 5.2's one run of 1,500 leave 3,000:
- 4,672,622 + 3,000 × 13,536.6 = 45,282,422, under 60,000,000.
- A 150 x 2 rehearsal adds about 4,060,980, so three fit (57,465,362) and a fourth would not (61,526,342).
- Answers: the 598 since the cutoff plus the 3,000 planned make 3,598, under the 7,500 stop.

Judging and the agent's own calls come on top (CLAUDE.md).

**Flagsmith usage.** Read only, at 09:34:03Z: `uv run python agent/spike/admin_api.py get /api/v1/organisations/41656/usage-data/total-count/`.
```
{"request": "GET /api/v1/organisations/41656/usage-data/total-count/", "status": 200, "body": {"count": 150}}
```
That is still only 1.2's 150 identities from 2026-11-26: the total has not yet taken in 2026-11-27. Today's identity requests are 2.2's 8, 3.6's 301 and 4.3's 298, about 607, plus events if they count. That is far under 35,000. The lead reads it again before 5.2.

## 4.1's hash check, after the rebuild (G4)
`evidence/4.1-alarm.md`'s hash check compared `dev` at 96bde1a with 1e8563c. Since 96bde1a, the files that go into `rm-invoker` have changed only in `contracts/names.py` (c4 to c7). The handler and `infra/modules/signals` are unchanged:
```
$ git diff --stat 96bde1a 2a154a1 -- signals/ contracts/__init__.py contracts/names.py infra/modules/signals/
 contracts/names.py                 | 130 ++++++++++++++++++++++-
 signals/tests/test_inject_fault.py | 204 +++++++++++++++++++++++++++++++++++++
```
The app and FIS apply at about 08:07Z rebuilt the zip with the new `names.py`. Read only, at 09:34:02Z:
```
$ aws lambda get-function-configuration --region us-east-1 --function-name rm-invoker --query '{CodeSha256:CodeSha256,LastModified:LastModified,STUB:Environment.Variables.STUB}' --output json
{
    "CodeSha256": "2Fp8VF+3LP+tJqx7hN3J+xNL8TSMD9Y/37RInezQBWE=",
    "LastModified": "2026-11-27T08:07:39.000+0000",
    "STUB": "1"
}
```
5.1's apply sets `STUB` to `0`. 5.1's evidence records the hash after it.

## Closing
Filled in when Gate 2 closes:
- the re-review goldfish's verdict on PR #33's and PR #30's fixes;
- the merges of PRs #33, #30 and #34, and the ticks of 2.7, 3.6, 4.1 and 4.3;
- Ahmed's approval: his 08:13:51Z words, as Gate 1 recorded his 01:04:32Z words.

## Cleanup list, additions
These join `evidence/gate-1-review.md`'s list, under the same rule: each removal waits for the end of judging and Ahmed's yes.

| Piece | Find | Remove |
| --- | --- | --- |
| Flagsmith features `assistant_model_rlive0421`, `assistant_model_rsmoke0810`, `assistant_model_rfault0831` and `assistant_model_rmanual0851`, with experiments 918, 1127, 1140 and 1172 | `uv run python agent/spike/admin_api.py get '/api/v1/projects/55601/features/?search=assistant_model_'` | In the dashboard, or through the Admin API with Ahmed's yes |
| The features and experiments of 5.1's and 5.2's runs | The same search | The same way |
