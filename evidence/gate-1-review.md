# Gate 1 review

docs/REVIEW.md puts Gate 1 after task 1.4. Its required evidence was all present:
- `evidence/1.2-*.md` and `evidence/1.3-*.md`;
- `evidence/1.4-results-sample.json`;
- a Result on every design.md section 13 row owned by 1.2, 1.3 or 1.4.

| | |
| --- | --- |
| Head reviewed | `lead/1.4-reconcile` at 2b09ca0 (PR #7). It merged into `dev` as c78b07d, with the third review's fix f32e75a |
| Reviewers | Two goldfish, run in parallel on 2026-11-27 from about 00:40Z to 01:15Z: the builder ("could you take over tomorrow?") and the sceptical judge ("is the evidence real?") |
| Verdicts | Both `pass`, with no blockers. The builder raised 14 shoulds and 4 nits, and the judge 6 shoulds and 9 nits |
| Human | Ahmed, in chat, 2026-11-27 |

## Ahmed's approval and written acceptance
The lead's last message before his reply said it would ask him for three things:
- to approve 1.4;
- to approve Gate 1;
- to accept in writing that the lead did his two dashboard steps (below).

His replies, verbatim:
- "yes, start A on 2.1 and B on 3.4"
- "it's auto mode so it's all acceptable to get the final results"

The lead records the second as his approval of 1.4 and Gate 1, and as his written acceptance of the dashboard steps.

**The lead's dashboard steps, a one-off.** On 2026-11-26 Ahmed asked, "do this via chrome". In his Chrome, the lead:
- read experiment 807's dashboard: at least 50 identities per arm, and the means;
- ended 807 at 23:19:47Z, with "Roll out control";
- ended 819 at 23:20:22Z, the same way.

These were the **Human** lines of tasks 1.2 and 1.3. CLAUDE.md's rule otherwise stands: the human ends every experiment, and no agent ends one.

## Findings and what was done
c1 is contract change c1 (PR #9) and c2 is contract change c2 (PR #12). "Carried" names the task or owner that closes a finding.

| Hat, item | Severity | Finding | Done |
| --- | --- | --- | --- |
| Judge 1 | should | Every load reused `user-001`…`user-150`, and nothing shows results count only an experiment's own events | c1: identities are per run, `<run_id>-user-NNN` |
| Judge 2 | should | The record closing the lead's Human check was missing | This file, above |
| Judge 3, builder 8 | should | The promote rule and the spec still used a "not worse" probability Flagsmith does not return | c2: the rule is the interval's lower bound at or above -0.05, stated as at least a 97.5 percent probability. `promote_min_prob_not_worse` and `prob_cheaper_not_worse` are removed, and the spec scenario is updated |
| Judge 4 | should | A value change and a kill switch's propagation were never run | c2: 2.2's live test does both and times publish to visible |
| Judge 5 | should | "Flagsmith has no primary-metric flag" is wrong for the dashboard | c2: "the API has no primary-metric field". 5.1 records what the dashboard shows |
| Judge 6, builder 6a | should | What "ended" means was not pinned | c2: status `completed`; `paused` is not ended (`EXPERIMENT_STATUS_ENDED`) |
| Judge 7 | nit | The lock claim was overgeneralised | c2: "the API accepted all three writes 1.3 tried" |
| Judge 8 | nit | `evidence/1.2-sdk-spike.md` lines 378 and 419 date the status read 23:20:45Z, which was the failed first attempt | Carried: B corrects them in its next PR. The file is B's |
| Judge 9 | nit | 1.4's raw logs were not committed | c2: `evidence/1.4-logs/`, with every 22-character token redacted. The environments list's responses carried both environment keys |
| Judge 10 | nit | 1.3's "no agent called an end-experiment endpoint" hides that the lead ended it | This file says so plainly. Carried: A rewords 1.3's step 12 in 2.7 |
| Judge 11 | nit | `helpful_lift_ci_lower`'s `minimum -1` would reject a valid interval | c2: the minimum is removed, and a -1.07 result validates |
| Judge 12 | nit | An identity first seen during a rollback is enrolled in `cheaper` without being served it | c2: section 10's Known bias names it, and so do 2.4's report and 6.2's README |
| Judge 13 | nit | 1.2 states a derived time "to the millisecond" | Carried: B rewords it with judge 8 |
| Judge 14 | nit | Side claims stronger than the run: Sonnet 5 as a "proven fallback" judge, PR #6's two-space claim, and "promote path is possible" | Carried: B rewords the first; the second is history. c2 says every planned run ends `hold, re-run`, and 6.2 and 6.3 claim no promote |
| Judge 15 | nit | Contract text | c2 covers the treatment split, the MCP tools row and router step 3's enabled check (1.2's finding 7). c1 covers the usage lag (re-read: 150 at 01:10Z). Docs citations for per-identity aggregation are left for 3.6's check |
| Builder 1 | should | The runtime's code layout was undecided | c1: `agent/runtime/`, package `rm_agent`, entrypoint `app.py` |
| Builder 2 | should | The `marshal` console script could never install | c2: the project is packaged from 2.6 (hatchling, `cli` and `contracts`), and the test runs `uv run marshal --help` |
| Builder 3 | should | Nothing pinned the no-end rule at the tool boundary | c2: section 4 and 2.2 refuse other tools, the five status fields, and any `complete/` or `pause/` path |
| Builder 4 | should | `set_tick` could drop the tick's input, and the schedule's group and role were unnamed | c1: the names. c2: 2.4 writes the schedule back whole, and a test checks the input |
| Builder 5 | should | The runtime's IAM role was undecided | c2: the stack's own role, found through the `ApplicationRuntimeRmAgentRoleArnOutput…` output, plus inline `rm-agent-runtime-tools` |
| Builder 6 | should | The Flagsmith spellings and rules lived only in the evidence | c2: statuses, the split rule, and the boundary names. Carried to 2.2: the spellings and the freshness rule, which the lead moves into names.py |
| Builder 7 | should | `rm-flagsmith-admin`'s interface was unspecified | Carried: 2.2 proposes it, and the lead records it in design.md section 7 before 2.2 merges |
| Builder 9 | should | Nothing pinned `answer_helpful` as an int | c2: a 3.3 test |
| Builder 10 | should | The review role was needed before any task said so | PR #8: `infra/review.tf`, and the `rm-review` profile in Ahmed's `~/.aws/config`. c1: reviewers use `--profile` |
| Builder 11 | should | Builds were written down nowhere | c2: `make build` runs each function's `build.sh`. Section 2 and 3.5 name it |
| Builder 12 | should | The cleanup list was incomplete | This file, below. c2: 6.4 works through it |
| Builder 13 | should | A newer AgentCore CLI rewrites the CDK dependencies | c2: before each deploy, 2.7 records the `package.json` diff and the CDK diff |
| Builder 14 | should | Gate 1's evidence commands ran only on the lead's machine | c1: CLAUDE.md names whose profile it is. c2: 1.4's evidence names an `admin_api.py` rerun |
| Builder 15 | nit | The deployed runtime's version is not recorded | c2: 2.7 records the version and commit after each deploy |
| Builder 16 | nit | Text drift | c2: (a) tools row, (e) preflight checks uv, node and jq, (g) 4.2's note, (i) `topK` forbidden. 2.7: (b) the `setup_gateway.py` docstring. Carried to 2.2: (d) the metric ID type. Left: (c) the spike's comment and (f) branch names in the spike evidence, both history |
| Builder 17 | nit | The usage reads may have been lag | c1: they were. CLAUDE.md reads the API again |
| Builder 18 | nit | The boto3 question and the Bedrock project grant | c1: botocore 1.42.97 has `InvokeAgentRuntime`, so 4.1 bundles nothing. The project grant is harmless and stays |

Per-task guesses, from the builder's Q3, not covered above. These are carried, and the lead settles each before its task starts:
- **2.3:** model display names.
- **2.6:** the ARN source and exit codes. Done in c2.
- **3.1:** the typed throttle error, and a helper for the dotted thinking fields.
- **3.2:** the judge after an answer error, and what happens when the experiment feature is missing.
- **3.3:** the shape of `prices.json`.
- **3.5:** the module's outputs, and `reset.py` with a seeded pointer.
- **3.6:** the baseline p95 query.
- **4.3:** the FIS bucket and role names.

## Cleanup list
Each removal waits for the end of judging and Ahmed's yes. Every remove is irreversible, so each one is confirmed when it is done. Find commands are read only.

| Piece | Find | Remove |
| --- | --- | --- |
| Stack `AgentCore-rm-default` (runtime `rm_default_rm_agent-5Izilk9O73` and its role) | `aws cloudformation describe-stacks --stack-name AgentCore-rm-default` | `aws cloudformation delete-stack --stack-name AgentCore-rm-default` |
| Gateway `rm-gateway-a1eznsykah`, its targets, `rm-gateway-role`, credential provider `rm-flagsmith-api-key` and its managed secret | `uv run python agent/scripts/setup_gateway.py --plan` | Delete the targets, then the Gateway, then the credential provider, through `bedrock-agentcore-control`. Then delete the role after detaching its policies |
| The runtime's and the Gateway's workload identities | `aws bedrock-agentcore-control list-workload-identities` | `delete-workload-identity` for each `rm` one |
| Log groups `/aws/bedrock-agentcore/runtimes/*`, `aws/spans`, `/aws/application-signals/*` | `aws logs describe-log-groups --log-group-name-prefix <prefix>` | `aws logs delete-log-group` for each |
| CloudWatch Transaction Search, switched on account-wide by `agentcore deploy`; Ahmed chose to keep it until cleanup | `aws xray get-trace-segment-destination` | Set the destination back to `XRay` |
| Stack `CDKToolkit`, with its KMS key and staging bucket | `aws cloudformation describe-stacks --stack-name CDKToolkit` | Empty the staging bucket, then delete the stack. The key is scheduled for deletion by AWS |
| Everything Terraform made from `infra/`, including `rm-review-readonly` | `terraform plan -destroy` from `infra/` | `terraform destroy` from `infra/`, by the lead |
| The state bucket `rm-tfstate-818928662830` | `aws s3 ls s3://rm-tfstate-818928662830` | After the destroy, empty it and delete it |
| Flagsmith objects from the spikes | `uv run python agent/spike/admin_api.py get '/api/v1/projects/55601/features/?search=spike_'` | In the dashboard, or through the Admin API with Ahmed's yes (list below) |
| Flagsmith organisation key `rollout-marshal-agent` | Flagsmith organisation settings | Ahmed deletes it after judging (design.md section 8). AgentCore Identity's copy stops working with it |
| Secrets `rm/flagsmith/api-key` and `rm/flagsmith/server-key` | `aws secretsmanager describe-secret` | Ahmed decides |
| Bedrock data retention mode `aws_review` (task 0.2) | `aws bedrock get-account-data-retention` | Ahmed decides |
| The `rm-review` profile in `~/.aws/config` | Its section | Ahmed removes it |

The Flagsmith spike objects are:
- **1.2:** features `spike_flag` (306302, option 37532), metric `spike_metric` (858), experiment 807;
- **1.3:** features 306314 (`assistant_model_rspike2235`, option 37546), 306318 and 306319 (option 37550), metric `spike_mcp_metric` (868), experiment 819.

## Review role (PR #8)
After the apply, the lead records the `simulate-principal-policy` results here and in PR #8:
- each deny must show `explicitDeny`;
- the matched statement must come from `rm-review-deny`, recording the exact source ID returned;
- `lambda:GetFunction` and `s3:ListBucket` on the state bucket must be `allowed`.

Contract change c2 does not wait for these; they follow the apply.
