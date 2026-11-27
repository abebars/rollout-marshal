"""Static checks on infra/modules/app (task 3.5).

terraform validate and plan prove the module works; these tests pin the settings a quiet
edit could lose. Whitespace is normalised, because terraform fmt aligns the `=`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from contracts import names

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "infra" / "modules" / "app"


def code(text: str) -> str:
    """The HCL without comments, every run of whitespace as one space."""
    lines = (line.split("#", 1)[0] for line in text.splitlines())
    return re.sub(r"\s+", " ", " ".join(lines))


FILES = {path.name: code(path.read_text()) for path in sorted(MODULE.glob("*.tf"))}
TF = " ".join(FILES.values())


def block(text: str, opening: str) -> str:
    """The {...} block that follows opening in text, braces balanced."""
    start = text.index(opening) + len(opening)
    start = text.index("{", start)
    depth = 0
    for index in range(start, len(text)):
        depth += {"{": 1, "}": -1}.get(text[index], 0)
        if depth == 0:
            return text[start : index + 1]
    raise AssertionError(f"unbalanced block after {opening}")


def function(key: str) -> str:
    """One entry of local.functions."""
    return block(block(FILES["locals.tf"], "functions ="), f"{key} =")


def test_the_module_has_exactly_the_leads_inputs_and_outputs():
    variables = re.findall(r'variable "(\w+)"', TF)
    assert variables == ["cheaper_extra_layers", "cheaper_extra_env"]
    assert 'variable "cheaper_extra_layers" {' in TF and "type = list(string) default = []" in TF
    assert "type = map(string) default = {}" in TF
    outputs = re.findall(r'output "(\w+)"', TF)
    assert outputs == [names.APP_ASK_URL_OUTPUT, "cheaper_function_name", "cheaper_role_name"]


def test_rm_router_and_rm_judge_log_json_at_info_never_debug():
    """The Flagsmith SDK logs identifiers at DEBUG. INFO, not WARN: Lambda gives the
    embedded-metric lines, JSON with no level, the level INFO."""
    assert 'sdk_log_format = "JSON"' in FILES["locals.tf"]
    assert 'sdk_log_level = "INFO"' in FILES["locals.tf"]
    for key in ("router", "judge"):
        entry = function(key)
        assert "log_format = local.sdk_log_format" in entry
        assert "log_level = local.sdk_log_level" in entry
    for key in ("current", "cheaper"):
        assert 'log_format = "Text"' in function(key) and "log_level = null" in function(key)
    logging = block(FILES["functions.tf"], "logging_config")
    assert "application_log_level = each.value.log_level" in logging
    assert "system_log_level = each.value.log_level" in logging
    assert "DEBUG" not in TF and "TRACE" not in TF


def test_the_judges_async_retries_stay_at_lambdas_default():
    """The judge's failure order relies on Lambda retrying it (task 3.3)."""
    assert "aws_lambda_function_event_invoke_config" not in TF
    assert "maximum_retry_attempts" not in TF


def test_each_function_runs_its_handler_from_its_zip_with_its_timeout():
    expected = {
        "current": ("local.answer_handler", "local.zips.answer", "ANSWER_TIMEOUT_SECONDS"),
        "cheaper": ("local.answer_handler", "local.zips.answer", "ANSWER_TIMEOUT_SECONDS"),
        "router": ("local.router_handler", "local.zips.router", "ROUTER_TIMEOUT_SECONDS"),
        "judge": ("local.judge_handler", "local.zips.judge", "JUDGE_TIMEOUT_SECONDS"),
    }
    for key, (handler, zip_path, timeout) in expected.items():
        entry = function(key)
        assert f"handler = {handler}" in entry, key
        assert f"zip = {zip_path}" in entry, key
        assert f"timeout = local.names.{timeout}" in entry, key
    locals_tf = FILES["locals.tf"]
    assert "answer_handler = local.names.ANSWER_HANDLER" in locals_tf
    assert "router_handler = local.names.ROUTER_HANDLER" in locals_tf
    assert 'judge_handler = "app.judge.handler.handler"' in locals_tf
    assert 'runtime = "python${local.names.PYTHON_VERSION}"' in TF
    assert "architectures = [local.names.LAMBDA_ARCHITECTURE]" in TF
    assert "source_code_hash = filebase64sha256(each.value.zip)" in TF


def test_each_answer_function_gets_its_model_and_the_cheaper_one_its_extras():
    assert "env = { (local.model_env) = local.names.MODEL_CURRENT }" in function("current")
    cheaper = function("cheaper")
    # The extras first, so they can never replace MODEL_ID.
    assert (
        "env = merge(var.cheaper_extra_env, { (local.model_env) = local.names.MODEL_CHEAPER })"
        in cheaper
    )
    assert "layers = var.cheaper_extra_layers" in cheaper
    for key in ("current", "router", "judge"):
        assert "layers = []" in function(key)
    assert "model_env = local.names.ANSWER_MODEL_ENV" in FILES["locals.tf"]


def test_the_api_is_one_aws_iam_route_with_payload_format_2():
    api = FILES["api.tf"]
    assert 'protocol_type = "HTTP"' in api
    assert api.count('resource "aws_apigatewayv2_route"') == 1
    assert "route_key = local.names.API_ROUTE_KEY" in api
    assert "authorization_type = local.names.API_AUTHORIZATION_TYPE" in api
    assert "payload_format_version = local.names.API_PAYLOAD_FORMAT_VERSION" in api
    assert 'integration_type = "AWS_PROXY"' in api
    assert 'name = "$default" auto_deploy = true' in api
    assert names.API_AUTHORIZATION_TYPE == "AWS_IAM" and names.API_ROUTE_KEY == "POST /ask"
    assert names.API_PAYLOAD_FORMAT_VERSION == "2.0"


def test_ask_url_is_the_routes_full_url():
    assert 'route_path = split(" ", local.names.API_ROUTE_KEY)[1]' in FILES["locals.tf"]
    assert (
        'value = "${aws_apigatewayv2_api.ask.api_endpoint}${local.route_path}"'
        in FILES["outputs.tf"]
    )


def test_the_sdk_key_is_readable_by_the_router_and_the_judge_only():
    functions = FILES["functions.tf"]
    documents = re.findall(r'data "aws_iam_policy_document" "(\w+)"', functions)
    readers = [
        name
        for name in documents
        if "secretsmanager:GetSecretValue"
        in block(functions, f'data "aws_iam_policy_document" "{name}"')
    ]
    assert readers == ["router", "judge"]
    attach = block(functions, 'data "aws_iam_policy_document" "fn"')
    assert 'each.key == "router" ? data.aws_iam_policy_document.router.json' in attach
    assert 'each.key == "judge" ? data.aws_iam_policy_document.judge.json' in attach
    assert "SECRET_FLAGSMITH_API_KEY" not in TF  # the organisation key is never in reach
    assert "secret_string" not in TF and "aws_secretsmanager_secret_version" not in TF


def test_the_router_may_invoke_exactly_the_answer_functions_and_the_judge():
    router = block(FILES["functions.tf"], 'data "aws_iam_policy_document" "router"')
    assert '[for key in ["current", "cheaper", "judge"] : local.function_arns[key]]' in router
    assert 'actions = ["dynamodb:GetItem"]' in router and "local.runs_table_arn" in router


def test_bedrock_goes_to_each_functions_own_profile_its_models_and_the_default_project():
    locals_tf = FILES["locals.tf"]
    callers = block(locals_tf, "bedrock_callers =")
    assert "current = local.names.MODEL_CURRENT" in callers
    assert "cheaper = local.names.MODEL_CHEAPER" in callers
    assert "judge = local.names.MODEL_JUDGE" in callers
    assert "router" not in callers  # the router never calls Bedrock
    project = "arn:aws:bedrock:${local.region}:${local.account_id}:project/default"
    assert f'default_project_arn = "{project}"' in locals_tf
    bedrock = block(FILES["functions.tf"], 'data "aws_iam_policy_document" "bedrock"')
    assert bedrock.count('actions = ["bedrock:InvokeModel"]') == 3
    assert 'variable = "bedrock:InferenceProfileArn"' in bedrock


def test_state_and_reports_come_from_names():
    storage = FILES["storage.tf"]
    assert "name = local.names.RUNS_TABLE" in storage
    assert "hash_key = local.names.PARTITION_KEY" in storage
    assert "bucket = local.names.REPORTS_BUCKET" in storage
    for setting in (
        "block_public_acls",
        "block_public_policy",
        "ignore_public_acls",
        "restrict_public_buckets",
    ):
        assert f"{setting} = true" in storage


def test_names_come_from_names_json_found_from_the_module_itself():
    locals_tf = FILES["locals.tf"]
    assert 'repo_root = "${path.module}/../../.."' in locals_tf
    assert 'names = jsondecode(file("${local.repo_root}/contracts/names.json"))' in locals_tf
    assert "path.root" not in TF
    generated = json.loads((ROOT / names.NAMES_JSON_FILE).read_text())
    assert generated["ANSWER_HANDLER"] == names.ANSWER_HANDLER


def test_the_module_pins_no_provider_and_creates_no_zip():
    assert 'required_providers { aws = { source = "hashicorp/aws" } }' in FILES["versions.tf"]
    assert "version" not in FILES["versions.tf"].replace("required_providers", "")
    assert "archive_file" not in TF  # app/build.sh builds the zips (tasks.md 3.5)


def test_iam_scope_and_route_permission_are_pinned():
    """PR #27's review: each loosening survived the checks above. A `*` for a profile, the
    models, the logs or the key; the key handed to another function; and a wider route
    permission each fail here."""
    functions = FILES["functions.tf"]
    attach = block(functions, 'data "aws_iam_policy_document" "fn"')
    assert re.findall(r'each\.key == "(\w+)" \?', attach) == ["router", "judge"]
    profile = "data.aws_bedrock_inference_profile.model[each.value]"
    raw = (MODULE / "functions.tf").read_text()
    assert re.findall(r"^\s*resources\s*=\s*(.+?)\s*$", raw, flags=re.M) == [
        f"[{profile}.inference_profile_arn]",
        f"[for model in {profile}.models : model.model_arn]",
        "[local.default_project_arn]",
        '["${local.log_group_arns[each.key]}:*"]',
        "[local.runs_table_arn]",
        "[data.aws_secretsmanager_secret.server_key.arn]",
        '[for key in ["current", "cheaper", "judge"] : local.function_arns[key]]',
        "[data.aws_secretsmanager_secret.server_key.arn]",
    ]
    assert TF.count("data.aws_secretsmanager_secret.server_key.arn") == 2  # router and judge
    assert (
        'source_arn = "${aws_apigatewayv2_api.ask.execution_arn}/*/'
        '${split(" ", local.names.API_ROUTE_KEY)[0]}${local.route_path}"'
    ) in FILES["api.tf"]
    locals_tf = FILES["locals.tf"]
    log_arns = block(locals_tf, "log_group_arns =")
    assert ':log-group:/aws/lambda/${fn.name}"' in log_arns
    table = "arn:aws:dynamodb:${local.region}:${local.account_id}:table/${local.names.RUNS_TABLE}"
    assert f'runs_table_arn = "{table}"' in locals_tf
    assert ':function:${fn.name}"' in block(locals_tf, "function_arns =")


def test_each_function_has_the_memory_c6_records():
    """256 MB, task 1.2's spike size, which c6 records."""
    assert "memory_mb = 256" in FILES["locals.tf"]
    assert "memory_size = local.memory_mb" in FILES["functions.tf"]


def test_each_role_gets_its_own_policy_with_exactly_these_actions():
    """PR #27's second review: a function given another's policy, a `logs:*` or a
    `lambda:*`, and a second permission each passed before."""
    functions = FILES["functions.tf"]
    attach = block(functions, 'resource "aws_iam_role_policy" "fn"')
    assert "role = aws_iam_role.fn[each.key].id" in attach
    assert "policy = data.aws_iam_policy_document.fn[each.key].json" in attach
    function = block(functions, 'resource "aws_lambda_function" "fn"')
    assert "role = aws_iam_role.fn[each.key].arn" in function
    raw = (MODULE / "functions.tf").read_text()
    assert re.findall(r"^\s*actions\s*=\s*(.+?)\s*$", raw, flags=re.M) == [
        '["sts:AssumeRole"]',
        '["bedrock:InvokeModel"]',
        '["bedrock:InvokeModel"]',
        '["bedrock:InvokeModel"]',
        '["logs:CreateLogStream", "logs:PutLogEvents"]',
        '["dynamodb:GetItem"]',
        '["secretsmanager:GetSecretValue"]',
        '["lambda:InvokeFunction"]',
        '["secretsmanager:GetSecretValue"]',
    ]
    assert TF.count('resource "aws_lambda_permission"') == 1
    permission = block(FILES["api.tf"], 'resource "aws_lambda_permission" "api"')
    assert 'action = "lambda:InvokeFunction"' in permission
    assert 'principal = "apigateway.amazonaws.com"' in permission
    assert 'function_name = aws_lambda_function.fn["router"].function_name' in permission
