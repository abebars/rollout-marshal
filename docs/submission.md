# Submission (task 6.4)

Owner: Ahmed. The lead keeps this checklist.
- Target: 2026-11-29T02:00:00Z.
- Hard cutoff: 2026-11-29T14:30:00Z.

## Checklist, in order
- [x] **The repo is public:** https://github.com/abebars/rollout-marshal, `visibility` PUBLIC, read with `gh repo view` on 2026-11-27.
- [ ] **Gates 3 and 4 approved,** and `dev` merged to `main`. The lead ticks this at the merge. Ahmed approved at 21:42:17Z on 2026-11-27 (`evidence/gate-4-review.md`). The video script is part of that (task 6.3).
- [ ] **The video is recorded,** on one new short run, following "Recording the run" below. Ahmed chose a short run at 22:28:26Z.
- [ ] **The run's record** is in `evidence/6.4/`, and the video script's placeholders are filled from it (the lead).
- [ ] **The video is uploaded,** and its URL replaces the README's `[[6.4: video URL]]`. The README's opening draft comment is deleted.
- [ ] **The post-recording change reaches `main`:** the README's video URL, its draft comment deleted, `evidence/6.4/` and the filled video script, in one pull request into `dev`. One goldfish reviews it as a per-task review (docs/REVIEW.md); then `dev` merges to `main`, with Ahmed's yes for that merge.
- [ ] **The entry is submitted** on WeMakeDevs, Track 2, with the repo URL and the video URL.

## Recording the run
One real run at 150 users x 2 questions, screen-recorded as it happens. The answer cost is about 3,500,000 micro-USD, which brings the spend to about 44,300,000 of the 60,000,000 stop; 40,812,020 was spent by the end of 5.2 (`evidence/5.2/r202611272042.md`). At 150 x 2, both arms still pass the 50-identity floor (task 3.6), and the fault still fires the alarm (task 4.3).

Each step, from the repo root, with the SSO profile in 818928662830:
1. **Reset:** `uv run python scripts/reset.py`. It must end with "no experiment is running".
2. **Start the run,** in the recorded terminal, after `mkdir -p evidence/6.4` (`--record` needs its folder, and refuses an existing file):
   `uv run python scripts/e2e.py --pause-minutes 180 --questions 2 --prompt "Test moving the support assistant from Claude Fable 5.1 to Claude Sonnet 5. Don't let answer quality or latency slip." --record evidence/6.4/record.json`
   `e2e.py` checks its preconditions, sends `marshal start` and prints the plan. After the launch, it starts `load.py` and `inject_fault.sh` itself.
3. **Approve, decline:** type each command `e2e.py` prints, in a second terminal: `uv run marshal approve <run_id> launch`, then `uv run marshal approve <run_id> resume`, then `uv run marshal decline <run_id>`. The run recommends `hold, re-run`.
4. **End the experiment** in the Flagsmith dashboard, on camera. The "Complete experiment" dialog pre-selects the cheaper variant: change it to **Roll out control**.
5. **Stop lines** (CLAUDE.md): run the two commands `e2e.py`'s summary prints, `uv run python evidence/3.6-scripts/smoke_reads.py cost …` and `… usage`.
6. **The record:** the lead saves the run's history, its four timestamps, the report key and the SNS IDs in `evidence/6.4/<run_id>.md`, as 5.2's record does. It also saves the alarm's OK → ALARM and ALARM → OK times, read with `aws cloudwatch describe-alarm-history --region us-east-1 --alarm-name rm-cheaper-latency-p95 --history-item-type StateUpdate --start-date <the run's start> --end-date <its end>`, as 5.2's row 15 did. It then fills the video script's `[[rec: …]]` placeholders.

## Open items for Ahmed (none blocks the submission)
1. **The quoted chat words.** Confirm or correct the words of his that `evidence/gate-2-review.md` quotes, with one comment on PR #36.
2. **Who ends experiments.** CLAUDE.md's rule that the human ends every experiment is unchanged. The lead ended these in the dashboard: 807 and 819 on 2026-11-26, at his "do this via chrome"; then 918, 1127, 1140, 1172, 1495 and 1503 on 2026-11-27, under his chat delegation. Decide whether CLAUDE.md should say so; an edit to it needs his approval.
3. **The SNS subscription,** optional. It was confirmed by the email link, so anyone holding its ARN can unsubscribe it without credentials (`evidence/4.1-alarm.md`). The fix is to replace it and confirm it with `AuthenticateOnUnsubscribe` true; that needs his inbox.
4. **The Flagsmith usage total.** Read it again once 2026-11-27's daily row closes: `uv run python evidence/3.6-scripts/smoke_reads.py usage`. Expect about 3,900 identity requests with the recorded run, plus events if they are billable (design.md section 13).

## After judging: cleanup
Remove what the cleanup lists name, with Ahmed's yes for each removal: `evidence/gate-1-review.md`, `evidence/gate-2-review.md`, and the additions in `evidence/5.1-e2e.md`, `evidence/5.2/r202611272042.md` and `evidence/6.4/`. Or record why an item stays. Three more:
- **SSM parameter `/rm/agent-runtime-arn`.** `deploy_agent.py` wrote it, outside Terraform: `aws ssm delete-parameter --name /rm/agent-runtime-arn`.
- **The reports bucket.** Empty `rm-reports-818928662830` before `terraform destroy`, or the destroy fails on a bucket that is not empty.
- **The recorded run's Flagsmith objects:** its feature `assistant_model_<run_id>` and its experiment.
