#!/usr/bin/env bash
# Start the FIS experiment rm-delay-cheaper and wait for it to end by itself (task 4.3,
# design.md section 9): 10000 ms added to every rm-answer-cheaper invocation for 6 minutes.
#
# The script never stops the experiment: the fault ends by its own duration, and no agent ends
# an experiment. If it is interrupted, the experiment keeps running to its end.
#
# Usage:   scripts/inject_fault.sh
# Needs:   AWS credentials for account 818928662830. Use the SSO profile, not rm-review: the
#          organisation's SCP denies FIS to the review role.
# Exits:   0 when the experiment completes; 1 when it ends in any other state; 2 before anything
#          starts (wrong account, or not exactly one template); 3 if it is still running after
#          RM_FIS_MAX_WAIT_SECONDS (default 1200), with the experiment left to finish.
# Tuning:  RM_FIS_POLL_SECONDS (default 15).
#
# ACCOUNT, REGION and TEMPLATE_NAME are contracts/names.py's ACCOUNT_ID, REGION and
# FIS_TEMPLATE; signals/tests/test_inject_fault.py checks that they match.
set -euo pipefail

ACCOUNT="818928662830"
REGION="us-east-1"
TEMPLATE_NAME="rm-delay-cheaper"
POLL_SECONDS="${RM_FIS_POLL_SECONDS:-15}"
MAX_WAIT_SECONDS="${RM_FIS_MAX_WAIT_SECONDS:-1200}"

utc() { date -u +%Y-%m-%dT%H:%M:%SZ; }
say() { echo "inject_fault: $*"; }
fail() { echo "inject_fault: $*" >&2; exit 2; }

# TZ=UTC makes the CLI print FIS's own timestamps in UTC.
aws_() { TZ=UTC aws "$@" --region "$REGION"; }

account="$(aws_ sts get-caller-identity --query Account --output text)"
[ "$account" = "$ACCOUNT" ] || fail "the credentials are for account $account, not $ACCOUNT; nothing started"

templates="$(aws_ fis list-experiment-templates \
  --query "experimentTemplates[?tags.Name=='$TEMPLATE_NAME'].id" --output text)"
[ "$templates" = "None" ] && templates=""
read -r -a ids <<<"$templates" || true
[ "${#ids[@]}" -eq 1 ] || fail "expected exactly one template tagged Name=$TEMPLATE_NAME, found ${#ids[@]}; nothing started"
template_id="${ids[0]}"
say "template $TEMPLATE_NAME is $template_id"

# A failed start exits here with the CLI's own status; nothing is running then.
started="$(aws_ fis start-experiment --experiment-template-id "$template_id" \
  --query 'experiment.[id,state.status]' --output text)"
IFS=$'\t' read -r experiment_id state <<<"$started"
say "experiment $experiment_id started at $(utc) (state $state)"

# The experiment runs whatever happens here, so a failed poll is retried, not fatal.
started_waiting="$(date +%s)"
status="unknown"
while true; do
  if polled="$(aws_ fis get-experiment --id "$experiment_id" \
    --query 'experiment.[state.status,startTime,endTime,state.reason]' --output text)"; then
    IFS=$'\t' read -r status start_time end_time reason <<<"$polled"
    case "$status" in
      completed | stopped | failed | cancelled) break ;;
    esac
    say "$(utc) state $status"
  else
    say "$(utc) poll failed; retrying"
  fi
  if [ $(($(date +%s) - started_waiting)) -ge "$MAX_WAIT_SECONDS" ]; then
    echo "inject_fault: experiment $experiment_id still running ($status) after ${MAX_WAIT_SECONDS}s; left to finish by its duration" >&2
    exit 3
  fi
  sleep "$POLL_SECONDS"
done

say "experiment $experiment_id ended at $(utc): $status (FIS start $start_time, end $end_time: $reason)"
[ "$status" = "completed" ] || exit 1
