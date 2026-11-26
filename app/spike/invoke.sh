#!/usr/bin/env bash
# Task 1.2, phase 2: invoke rm-spike-sdk once for one action and keep its response as evidence.
#
#   app/spike/invoke.sh selfcheck  load the SDK and its compiled wheels; no secret, no request
#                                                                      -> evidence/1.2-selfcheck.json
#   app/spike/invoke.sh router     invocation 1, the router's role   -> evidence/1.2-router.json
#   app/spike/invoke.sh judge      invocation 2, the judge's role    -> evidence/1.2-judge.json
#                                  (assignments read from evidence/1.2-router.json, so the
#                                  judge invocation makes no identity request)
#   app/spike/invoke.sh converse   the Bedrock calls                  -> evidence/1.2-converse.json
#
# Every run checks the AWS account first, and never retries an invoke (AWS_MAX_ATTEMPTS=1 and
# a long read timeout), so a slow invocation is never sent twice. The function's response,
# StatusCode and log tail go to evidence/1.2-invoke-log.txt. The SDK key never reaches this
# shell: the function reads it itself, and its response is checked for redaction.
set -euo pipefail

ACTION=${1:-}
case "$ACTION" in
selfcheck | router | judge | converse) ;;
*)
  echo "usage: $0 selfcheck|router|judge|converse" >&2
  exit 2
  ;;
esac

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)
EVIDENCE="$ROOT/evidence"
WORK="$HERE/build/invoke"
mkdir -p "$WORK"

value() { (cd "$ROOT" && uv run --frozen python -c "from contracts import names; print(names.$1)"); }
ACCOUNT=$(value ACCOUNT_ID)
REGION=$(value REGION)
FUNCTION=$(terraform -chdir="$ROOT/infra/spike" output -raw function_name)

caller=$(aws sts get-caller-identity --query Account --output text)
if [ "$caller" != "$ACCOUNT" ]; then
  echo "refusing: AWS account is $caller, not $ACCOUNT" >&2
  exit 1
fi

case "$ACTION" in
selfcheck) jq -n '{action: "selfcheck"}' >"$WORK/payload.json" ;;
router) jq -n '{action: "router"}' >"$WORK/payload.json" ;;
judge) jq '{action: "judge", assignments: .assignments}' "$EVIDENCE/1.2-router.json" >"$WORK/payload.json" ;;
converse) jq -n '{action: "converse", include_optional: true}' >"$WORK/payload.json" ;;
esac

started=$(date -u +%Y-%m-%dT%H:%M:%SZ)
AWS_MAX_ATTEMPTS=1 aws lambda invoke --region "$REGION" --function-name "$FUNCTION" \
  --cli-read-timeout 900 --log-type Tail \
  --payload "fileb://$WORK/payload.json" "$WORK/response.json" >"$WORK/meta.json"
finished=$(date -u +%Y-%m-%dT%H:%M:%SZ)

{
  echo "== $ACTION: aws lambda invoke --function-name $FUNCTION, $started to $finished"
  jq '{StatusCode, FunctionError, ExecutedVersion}' "$WORK/meta.json"
  jq -r '.LogResult // empty' "$WORK/meta.json" | base64 -d
  echo
} >>"$EVIDENCE/1.2-invoke-log.txt"

if jq -e '.FunctionError' "$WORK/meta.json" >/dev/null; then
  echo "the function failed; see evidence/1.2-invoke-log.txt and $WORK/response.json" >&2
  exit 1
fi
if jq -e 'has("redaction_applied")' "$WORK/response.json" >/dev/null; then
  echo "the response needed redaction: not saved as evidence; tell the lead" >&2
  exit 1
fi
jq . "$WORK/response.json" >"$EVIDENCE/1.2-$ACTION.json"

case "$ACTION" in
selfcheck)
  jq '{ok, modules, versions, container}' "$EVIDENCE/1.2-selfcheck.json"
  ;;
router)
  jq '{aborted, arm_counts, samples: (.samples | map_values(select(.) | {identity, value, variant, enabled, experiment})),
       variants_by_arm, experiment_ids, not_in_experiment, exposures_tracked, events_delivered,
       delivered_before_return, events_refused, identity_requests, event_posts, key_is_server_side,
       seconds, sdk_log}' "$EVIDENCE/1.2-router.json"
  ;;
judge)
  jq '{expected, identity_requests, events_tracked, events_delivered, delivered_before_return,
       events_refused, event_posts, key_is_server_side, seconds, sdk_log, client: .client.instance}' \
    "$EVIDENCE/1.2-judge.json"
  ;;
converse)
  jq '{required_ok, calls: [.calls[] | {name, ok, stop_reason, wall_ms, bedrock_latency_ms, usage,
       content_blocks, error, text: ((.text // "")[0:120])}]}' "$EVIDENCE/1.2-converse.json"
  ;;
esac
