# Kickoff

## Before you start Claude Code (Ahmed)

Section 0 of `tasks.md` lists what has to be true. In short:

1. A shell where `aws sts get-caller-identity` prints account
   `818928662830` and the credentials last the whole session.
2. Data retention mode `aws_review` in us-east-1 in that account. The
   lead agent sets it during preflight if it is not already set.
3. Two secrets in Secrets Manager, us-east-1. Run these yourself in a
   terminal signed in to Sandbox-2, pasting each value at the prompt so
   it never lands in shell history:

   ```
   read -s V && aws secretsmanager create-secret --region us-east-1 \
     --name rm/flagsmith/api-key --secret-string "$V"; unset V
   read -s V && aws secretsmanager create-secret --region us-east-1 \
     --name rm/flagsmith/server-key --secret-string "$V"; unset V
   ```

   First value: the Flagsmith key `rollout-marshal-agent` you copied.
   Second value: the Development environment's client-side key from the
   Flagsmith SDK Keys page.
4. The email address for incident notes.
5. Track 2 selected on WeMakeDevs.
6. `git clone https://github.com/abebars/rollout-marshal` and copy this
   handover folder's contents into it. Do not commit yet.

Start no earlier than Wed Nov 25, 9:30 PM US Eastern
(`2026-11-26T02:30:00Z`).

## Prompt to paste into Claude Code

```
You are the integration lead for Rollout Marshal. Read CLAUDE.md, then the
four documents it lists, in order. Do not write any code until you have.

Then:

1. Run `date -u`. If it is before 2026-11-26T02:30:00Z, stop and tell me.
2. Run `aws sts get-caller-identity`. If the account is not 818928662830,
   stop and tell me.
3. Do tasks 1.0 and 1.1 yourself. Have a goldfish review the 1.0 commit
   before you push it. Stop at the 1.1 gate and show me the contracts
   pull request.
4. After I approve, start workstream agents A and B in separate git
   worktrees for the spikes 1.2 and 1.3, in parallel.
5. Do 1.4, run Gate 1 as docs/REVIEW.md describes, and stop for my
   approval.
6. Then run workstreams A, B and C in parallel, one task per agent at a
   time, each task reviewed by a fresh goldfish reviewer before you merge
   it. Start workstream D once task 5.1 passes. Keep tasks.md ticked as
   the task board.
7. You never run `marshal approve` or `marshal decline` yourself, in any
   script or test against the deployed agent. Those are mine.
8. Stop for me at every gate, and whenever CLAUDE.md says to ask.

My incident email is: <fill in>.
Report progress as a short table: task, owner, state, blocker.
```

## Sample prompt for the demo (used by task 2.3's test)

```
Test moving the support assistant from Claude Fable 5.1 to Claude Sonnet 5.
Don't let answer quality or latency slip.
```

## Where the plan lives

The approved design and plan this handover was written from:
https://claude.ai/artifact/4WpzpF8WMKuUrxKUateW2h
