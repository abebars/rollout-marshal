# Proposal: Build Rollout Marshal

## Why

Moving a production assistant to a cheaper model is a real cost decision
that teams make by hand today: set up flags, wire metrics, watch
dashboards, decide in a meeting. This change builds an agent that does the
whole loop from one prompt and keeps a human on every risky decision. It is
the entry of team Built Not Theorized in the Toggle Wars hackathon,
Track 2.

## What Changes

- A new agent on Amazon Bedrock AgentCore that plans, launches, monitors,
  rolls back, resumes and reports on a Flagsmith experiment.
- A small assistant API on Lambda and Bedrock that serves as the system
  under test, with a load generator.
- A signal path: CloudWatch alarm, EventBridge, a scheduled tick, SNS.
- A repeatable injected fault with AWS Fault Injection Service.
- A command line for the engineer, a README, and a demo video script.

## Capabilities

- `experiment-agent`: the agent, its state machine, tools and CLI.
- `assistant-api`: router, answer functions, judge, load generator.
- `signals`: alarm, wake-up, tick, notifications, fault injection.

## Impact

- New AWS resources in Sandbox-2 (`818928662830`), us-east-1 only.
- New features, metrics and experiments in Flagsmith project 55601.
- Bedrock spend for up to 7,500 answers plus judging.
- No existing system is modified.

## Out of scope

Multiple agents in the product itself, EKS, a web UI, real users, Slack.

## Deadlines

- No commit before `2026-11-26T02:30:00Z` (Wed Nov 25, 9:30 PM US Eastern).
- Submission cutoff `2026-11-29T14:30:00Z` (Sun Nov 29, 9:30 AM US
  Eastern). Target: submitted by `2026-11-29T02:00:00Z`.
