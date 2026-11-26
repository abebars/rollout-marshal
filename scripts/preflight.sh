#!/usr/bin/env bash
# Preflight for Rollout Marshal (tasks.md 1.0). Prints one "ok" or "FAIL" line
# per check and exits 1 if any check fails.
#
# Read-only, with one exception: if the Bedrock data retention mode in
# us-east-1 is not aws_review, it sets it (tasks.md 0.2: approved by Ahmed on
# Nov 24) and the ok line says so. It never reads a secret value.
# Compatible with the bash 3.2 that ships with macOS.
set -u

ACCOUNT="818928662830"
REGION="us-east-1"
START_UTC="2026-11-26T02:30:00Z"
START_NUM="20261126023000"
REPO="abebars/rollout-marshal"
SECRETS="rm/flagsmith/api-key rm/flagsmith/server-key"
MODELS="us.anthropic.claude-fable-5-1 us.anthropic.claude-sonnet-5 us.anthropic.claude-haiku-5-5"
TERRAFORM_MIN="1.10"

failures=0
ok() { printf 'ok   %s\n' "$*"; }
fail() {
  printf 'FAIL %s\n' "$*"
  failures=$((failures + 1))
}
utc_now() { date -u +%Y-%m-%dT%H:%M:%SZ; }
one_line() { tr '\n' ' ' | sed -E 's/ +/ /g; s/^ //; s/ $//' | cut -c1-200; }

# 1. Time.
now_num=$(date -u +%Y%m%d%H%M%S)
if [ "$now_num" -ge "$START_NUM" ]; then
  ok "utc time $(utc_now) is at or after $START_UTC"
else
  fail "utc time $(utc_now) is before $START_UTC"
fi

# 2. Account. Every later AWS check is skipped unless this passes, so the
# script never touches another account.
caller=$(aws sts get-caller-identity --query Account --output text 2>&1)
if [ "$caller" = "$ACCOUNT" ]; then
  ok "aws account is $ACCOUNT"
  account_ok=1
elif printf '%s' "$caller" | grep -Eq '^[0-9]{12}$'; then
  fail "aws account is $caller, expected $ACCOUNT"
  account_ok=0
else
  fail "aws credentials not usable: $(printf '%s' "$caller" | one_line)"
  account_ok=0
fi

# 3. Bedrock data retention mode (needed by the Fable 5.1 control arm).
if [ "$account_ok" -ne 1 ]; then
  fail "bedrock data retention mode: skipped, account check failed"
else
  mode=$(aws bedrock get-account-data-retention --region "$REGION" --query mode --output text 2>/dev/null)
  if [ "$mode" = "aws_review" ]; then
    ok "bedrock data retention mode is aws_review in $REGION"
  elif [ -z "$mode" ]; then
    fail "bedrock data retention mode could not be read in $REGION"
  elif aws bedrock put-account-data-retention --region "$REGION" --mode aws_review >/dev/null 2>&1 &&
    [ "$(aws bedrock get-account-data-retention --region "$REGION" --query mode --output text 2>/dev/null)" = "aws_review" ]; then
    ok "bedrock data retention mode set to aws_review in $REGION at $(utc_now) (was $mode; tasks.md 0.2)"
  else
    fail "bedrock data retention mode is '$mode' in $REGION and could not be set to aws_review"
  fi
fi

# 4. Secrets exist and have a current version. Metadata only, never the value.
for s in $SECRETS; do
  if [ "$account_ok" -ne 1 ]; then
    fail "secret $s: skipped, account check failed"
    continue
  fi
  stages=$(aws secretsmanager describe-secret --region "$REGION" --secret-id "$s" \
    --query 'VersionIdsToStages.*[]' --output text 2>/dev/null)
  deleted=$(aws secretsmanager describe-secret --region "$REGION" --secret-id "$s" \
    --query 'DeletedDate' --output text 2>/dev/null)
  if printf '%s' "$stages" | grep -qw AWSCURRENT && [ "$deleted" = "None" ]; then
    ok "secret $s exists with a current version (value not read)"
  else
    fail "secret $s is missing, scheduled for deletion, or has no AWSCURRENT version"
  fi
done

# 5. One Converse call per model. No temperature, top_p or top_k. maxTokens
# matches the answer budget in design.md section 3; a max_tokens stop fails.
for m in $MODELS; do
  if [ "$account_ok" -ne 1 ]; then
    fail "converse $m: skipped, account check failed"
    continue
  fi
  out=$(aws bedrock-runtime converse --region "$REGION" --model-id "$m" \
    --messages '[{"role":"user","content":[{"text":"Reply with the single word: ok"}]}]' \
    --inference-config '{"maxTokens":2000}' \
    --query '[stopReason, metrics.latencyMs, length(output.message.content[?text])]' \
    --output text 2>&1)
  stop=""
  latency=""
  ntext=""
  read -r stop latency ntext <<<"$out"
  if [ "$stop" = "end_turn" ] && [ "${ntext:-0}" -ge 1 ] 2>/dev/null; then
    ok "converse $m: end_turn with text in ${latency} ms"
  else
    fail "converse $m: $(printf '%s' "$out" | one_line)"
  fi
done

# 6. AgentCore control plane and FIS answer in us-east-1.
if [ "$account_ok" -ne 1 ]; then
  fail "agentcore control plane: skipped, account check failed"
  fail "fis: skipped, account check failed"
else
  if aws bedrock-agentcore-control list-agent-runtimes --region "$REGION" --max-results 1 >/dev/null 2>&1; then
    ok "agentcore control plane answers in $REGION"
  else
    fail "agentcore control plane did not answer in $REGION"
  fi
  if aws fis list-experiment-templates --region "$REGION" --max-results 1 >/dev/null 2>&1; then
    ok "fis answers in $REGION"
  else
    fail "fis did not answer in $REGION"
  fi
fi

# 7. GitHub CLI signed in, with push rights to the repo. Token never printed.
if gh auth status >/dev/null 2>&1; then
  perm=$(gh repo view "$REPO" --json viewerPermission -q .viewerPermission 2>/dev/null)
  case "$perm" in
  ADMIN | MAINTAIN | WRITE) ok "gh auth status ok with $perm on $REPO" ;;
  *) fail "gh auth status ok but permission on $REPO is '${perm:-unknown}'" ;;
  esac
else
  fail "gh auth status: not signed in"
fi

# 8. Terraform, at least the version the S3 backend's native locking needs.
if command -v terraform >/dev/null 2>&1; then
  tf=$(terraform version 2>/dev/null | head -1 | sed -E 's/^Terraform v//')
  tf_major=$(printf '%s' "$tf" | cut -d. -f1)
  tf_minor=$(printf '%s' "$tf" | cut -d. -f2)
  min_major=$(printf '%s' "$TERRAFORM_MIN" | cut -d. -f1)
  min_minor=$(printf '%s' "$TERRAFORM_MIN" | cut -d. -f2)
  if [ "${tf_major:-0}" -gt "$min_major" ] 2>/dev/null ||
    { [ "${tf_major:-0}" -eq "$min_major" ] && [ "${tf_minor:-0}" -ge "$min_minor" ]; } 2>/dev/null; then
    ok "terraform $tf"
  else
    fail "terraform $tf is older than $TERRAFORM_MIN"
  fi
else
  fail "terraform: not installed"
fi

# 9. AgentCore CLI (npm package @aws/agentcore).
cli=$(agentcore --version 2>/dev/null | head -1)
if [ -n "$cli" ]; then
  ok "agentcore cli $cli"
else
  fail "agentcore cli: not installed or not answering (npm install -g @aws/agentcore)"
fi

[ "$failures" -eq 0 ]
