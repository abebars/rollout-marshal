# Submission (task 6.4)

Owner: Ahmed. The lead keeps this checklist.
- Target: 2026-11-29T02:00:00Z.
- Hard cutoff: 2026-11-29T14:30:00Z.

## Checklist
- [x] **The repo is public:** https://github.com/abebars/rollout-marshal, `visibility` PUBLIC, read with `gh repo view` on 2026-11-27.
- [ ] **The README is final on `main`:** after Gate 4, `dev` is merged to `main`.
- [ ] **Ahmed approves the video script,** `docs/video-script.md`. Task 6.3's Pass is his approval.
- [ ] **The video is recorded and uploaded,** and its URL replaces the README's `[[6.4: video URL]]`.
- [ ] **The entry is submitted** on WeMakeDevs, Track 2, with the repo URL and the video URL.

## Recording the run
The video script asks for one real run, recorded as it happens. Its size sets the answer cost. CLAUDE.md's stop is 60,000,000 micro-USD of answers, and 40,812,020 was spent by the end of 5.2 (`evidence/5.2/r202611272042.md`).

| Size | Answers | Answer cost (from 5.2's run) | Spent afterwards |
| --- | --- | --- | --- |
| 150 x 10, as 5.1 and 5.2 | 1,500 | about 17,550,000 | about 58,360,000: under the stop, but close, and judging and the agent's own calls come on top |
| 150 x 2, as the smoke and the fault run | 300 | about 3,500,000 | about 44,300,000 |

At 150 x 2, both arms still pass the 50-identity floor (3.6), and the fault still fires the alarm (4.3).

**Steps:**
1. The lead runs `uv run python scripts/reset.py`.
2. Start the run: `uv run python scripts/e2e.py --pause-minutes 180 --questions 2 --record <new file>.json`. Leave out `--questions 2` for full size.
3. Ahmed types the launch, the resume and the decline as `e2e.py` prints them.
4. At the end, Ahmed ends the experiment in the Flagsmith dashboard, choosing "Roll out control" on camera. The dialog pre-selects the cheaper variant, so he must change it.

## Open items for Ahmed (none blocks the submission)
1. **The quoted chat words.** Confirm or correct the words of his that `evidence/gate-2-review.md` quotes, with one comment on PR #36. The lead's human steps rest on them.
2. **Who ends experiments.** CLAUDE.md's rule that the human ends every experiment is unchanged. Under his chat delegation the lead ended 918, 1127, 1140, 1172, 1495 and 1503 in the dashboard. Decide whether CLAUDE.md should say so; an edit to it needs his approval.
3. **The SNS subscription,** optional. It was confirmed by the email link, so anyone holding its ARN can unsubscribe it without credentials (`evidence/4.1-alarm.md`). The fix is to replace it and confirm it with `AuthenticateOnUnsubscribe` true; that needs his inbox.
4. **The Flagsmith usage total.** Read it again once 2026-11-27's daily row closes: `uv run python evidence/3.6-scripts/smoke_reads.py usage`. Expect about 3,600 identity requests, plus events if they are billable (design.md section 13).

## After judging: cleanup
Remove what the cleanup lists name, with Ahmed's yes for each removal: `evidence/gate-1-review.md`, `evidence/gate-2-review.md`, and the additions in `evidence/5.1-e2e.md` and `evidence/5.2/r202611272042.md`. Or record why an item stays.
