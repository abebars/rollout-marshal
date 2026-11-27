# Rollout Marshal: rules for every agent in this repo

Read in this order before doing anything:

1. `openspec/changes/build-rollout-marshal/proposal.md`
2. `openspec/changes/build-rollout-marshal/design.md` (the contract)
3. the three `specs/*/spec.md` files beside it
4. `openspec/changes/build-rollout-marshal/tasks.md`
5. `docs/REVIEW.md`

## Hard rules

- No commit before `2026-11-26T02:30:00Z`. Check `date -u` first. This is
  a hackathon rule, and breaking it can disqualify the entry.
- Submission cutoff is `2026-11-29T14:30:00Z`. Target is
  `2026-11-29T02:00:00Z`.
- Work only in AWS account `818928662830`, region `us-east-1`. Run
  `aws sts get-caller-identity` before any AWS write. If the account is
  anything else, stop and tell the human. The profile name
  `abisb_IsbAdminsPS-818928662830` is Ahmed's; any credentials for this
  account work the same.
- Never print, log, commit or paste a secret value, access key or token.
  Check that a secret exists; never read it back to the conversation.
- Never create IAM users or long-lived access keys. The human supplies
  the credentials you run with.
- Every name and value comes from `design.md`. Do not invent or rename.
  If the design is wrong, open a contract-change pull request.
- The agent never promotes a model, resumes traffic or launches an
  experiment without the human's `approve`. This covers `rm_agent`'s runs.
  A build agent starts only the scratch experiments a task names: 1.3's on
  `assistant_model_rspike<HHMM>`, and those on run IDs `rlive<HHMM>` (2.2),
  `rsmoke<HHMM>` (3.6) and `rfault<HHMM>` (4.3). Any other start needs the
  human's yes. The human ends every experiment; no agent ends one or calls
  an end-experiment endpoint.
- Do not send `temperature`, `top_p` (Converse: `topP`) or `top_k` to any
  Bedrock model.
- Stay in your workstream's directories (design.md section 12).
- Technical claims come from official documentation (docs.aws.amazon.com,
  docs.flagsmith.com) or from something you ran. If you could not verify
  it, say so in the pull request.

## Roles

- **Lead (integration)**: one agent. Owns `dev`, `infra/` applies (except
  task 1.2's `infra/spike/`, design.md section 2), `contracts/`, this file
  (each edit approved by the human), the task board and the gates. Starts
  the workstream agents and the goldfish reviewers. Does not write
  workstream code.
- **Workstream agents A, B, C, D**: one each, in its own git worktree and
  branch `ws-<letter>/<task-id>-<slug>`. Implements one task at a time.
- **Goldfish reviewers**: fresh agents with no history, started per review
  as docs/REVIEW.md describes.
- **Human (Ahmed)**: approves the contracts, each gate, each merge to
  `main`; enters secrets; ends experiments in the Flagsmith dashboard;
  plays the engineer in the demo; records and submits.

## How a task runs

1. Lead assigns the next unblocked task to its workstream agent.
2. The agent writes the failing test, then the code, runs the Pass check,
   and opens a pull request into `dev` whose description ends with a short
   walkthrough: what changed, why, how to see it work.
3. Lead starts a goldfish reviewer (docs/REVIEW.md, per-task review).
4. Fixes, then lead merges and ticks the box in `tasks.md`.
5. At each gate the lead runs the two-goldfish gate review and waits for
   the human.

## When to stop and ask the human

- A Pass check cannot be made green within the task's timebox.
- A spike contradicts `design.md`.
- Anything needs a secret, a sign-in, an approval, or a click in the
  Flagsmith dashboard.
- Flagsmith request usage passes 35,000. Read it after each load with
  `uv run python agent/spike/admin_api.py get
  /api/v1/organisations/41656/usage-data/total-count/`. The count lags by
  more than 25 minutes (Gate 1), so read it again later, or ask the human
  to read the Usage page.
- The CloudWatch sum of `AnswerCostMicroUsd` passes 60,000,000 (60 US
  dollars of answers; judging and the agent's own calls come on top). The
  sum is the Sum for `Variant=current` plus the Sum for `Variant=cheaper`
  since `2026-11-26T02:30:00Z` (statistic Sum, period 3600, all datapoints
  added). An empty result means unknown, not zero: fix the query first.
  Check it after every load.
- Anything would touch an account other than `818928662830`.

Ask with one specific question and the action you need. Keep working on
other unblocked tasks while you wait.

## Cut order if the build runs behind

1. Stretch items (none are in `tasks.md`; do not add any).
2. Task 5.2 reduced to one further clean run.
3. Task 6.1's manual baseline reduced to the step list with one timed pass.
4. FIS, replaced by the `chaos_delay_cheaper` flag fallback.
5. The EventBridge alarm path, replaced by `marshal check` run by hand.
6. Never cut: the approval gates, the Flagsmith experiment, the report.
