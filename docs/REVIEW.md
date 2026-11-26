# Review protocol: elephant and goldfish

The lead is the elephant: it holds the whole project in context and
decides. Reviewers are goldfish: fresh agents with no conversation
history, given only what a stranger would have. A goldfish catches what
the author can no longer see. Every step gets one.

## Rule

Nothing merges into `dev` without a goldfish approval. Nothing merges into
`main` without a gate review and the human's approval.

## Bootstrap

Task 1.0's commit is the only one made straight to `main`. A goldfish
reviews it before the push, given the handover files and the preflight
output. `dev` is created from it, and every later change is a pull
request into `dev`.

## Per-task review

1. The implementer opens a pull request for one task.
2. The lead starts a new agent with NO prior context and gives it only:
   - the task text from `tasks.md`,
   - `design.md`,
   - the matching `specs/*/spec.md`,
   - the diff,
   - the output of the task's Pass check.
3. The goldfish answers these, and nothing else:
   - Does the diff do what the task says, all of it, and nothing beyond it?
   - Does every name and value match `design.md`? List each mismatch.
   - Does each scenario in the spec that this task owns have a test? Name
     the missing ones.
   - Is the Pass output real (the command shown, the result green), or
     only claimed?
   - Is a secret value, account credential or token present anywhere in
     the diff or the logs?
   - What input or sequence would break this that no test covers? Give at
     most three, most likely first.
   - Verdict: `approve`, or `changes` with a numbered list.
4. The implementer fixes every numbered item or replies with a reason the
   lead accepts. A second goldfish (a new one, not the first) re-reviews
   only if code changed in response.
5. The lead merges to `dev`.

For tasks whose result is deployed state and not a diff (applies, 2.7,
3.6, 4.3, 5.1, 5.2), the goldfish also gets read-only AWS access and the
exact commands in the evidence file, and reruns them. An evidence file
without the commands that produced it is rejected. Each evidence file
marks its commands read or write. The goldfish first checks that
`AWS_PROFILE=rm-review aws sts get-caller-identity` shows the role
`rm-review-readonly`, then reruns only the read commands, each with that
profile, and never runs seed, load, inject, reset, `marshal` or
`terraform`. The lead reruns any Flagsmith reads and gives the goldfish
the command and the raw output.

A goldfish never edits code. An implementer never reviews its own task.
The same agent instance never reviews twice.

## Gate reviews

Each gate uses TWO goldfish with different hats, run in parallel, each
given `design.md`, `tasks.md`, the `evidence/` files for the gate, and
read access to the repo at `dev`.

- Hat 1, the builder: could you take over tomorrow from the repo alone?
  What is undocumented, untested, or only works on one agent's machine?
- Hat 2, the sceptical judge: is the evidence real? What is oversold,
  staged, or would not survive a question from a Flagsmith engineer?

| Gate | When | Evidence required | Then |
| --- | --- | --- | --- |
| 1 | After task 1.4 | `evidence/1.2-*.md`, `evidence/1.3-*.md`, `evidence/1.4-results-sample.json`, every row of design.md section 13 owned by 1.2, 1.3 or 1.4 resolved | Human approves; A, B, C continue |
| 2 | After 2.5, 3.6 and 4.2 | unit suites green, `evidence/3.6-*.md`, `evidence/4.2-*.md` | Human approves; integration starts |
| 3 | After 5.1 | `evidence/5.1-e2e.md` with the full state history | Human approves; merge `dev` to `main` |
| 4 | After 5.2, 6.1, 6.2, 6.3 | three run histories, README, video script, time-saved file | Human approves; merge `dev` to `main`; Human records, then submits |

The lead writes each gate's findings and what was done about each one to
`evidence/gate-<n>-review.md`. A finding is closed only by a change or by
the human accepting it in writing.

## Contract changes

A change to `design.md` or `contracts/` is its own pull request, reviewed
by a goldfish given the old and new text and the list of tasks that
consume the changed name. It needs the human's approval before merge, and
the lead tells every workstream agent what changed.

## What the human must be able to do

The hackathon requires that the entrant understands the code. Every pull
request description therefore ends with a short walkthrough: what changed,
why, and how to see it work. The human reads these at each gate.
