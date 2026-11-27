locals {
  # The repository root, from this module's own path, so any root can call the module.
  repo_root = "${path.module}/../../.."

  # Every name and value comes from contracts/names.json, generated from contracts/names.py.
  names      = jsondecode(file("${local.repo_root}/contracts/names.json"))
  account_id = local.names.ACCOUNT_ID
  region     = local.names.REGION

  # Handlers keep the repo's paths: the module's path plus the function (design.md
  # section 5). The judge's is not in contracts/names.py yet; it follows the same rule.
  answer_handler = local.names.ANSWER_HANDLER
  router_handler = local.names.ROUTER_HANDLER
  judge_handler  = "app.judge.handler.handler"
  model_env      = local.names.ANSWER_MODEL_ENV

  # Not in design.md, for the lead's next contract change. The roles, policies and log
  # groups follow rm-invoker's names (functions.tf).
  api_name = "rm-api"

  # app/build.sh writes the three zips here; the two answer functions share one.
  build_dir = "${local.repo_root}/app/build"
  zips = {
    answer = "${local.build_dir}/rm-answer.zip"
    router = "${local.build_dir}/rm-router.zip"
    judge  = "${local.build_dir}/rm-judge.zip"
  }

  # 256 MB, as task 1.2's spike function ran the same SDK and Converse calls; design.md
  # gives no size.
  memory_mb = 256

  # rm-router and rm-judge log in JSON at INFO, never DEBUG, because the Flagsmith SDK logs
  # identifiers at DEBUG. Log-level filtering needs the JSON format (Lambda developer guide,
  # log-level filtering). Lambda gives a print() of a JSON object with no "level" the level
  # INFO, so the embedded-metric lines need INFO: WARN would filter them out (same page).
  # print() output stays as printed (Python logging page), so CloudWatch still reads it.
  sdk_log_format = "JSON"
  sdk_log_level  = "INFO"

  functions = {
    current = {
      name       = local.names.ANSWER_CURRENT_FUNCTION
      zip        = local.zips.answer
      handler    = local.answer_handler
      timeout    = local.names.ANSWER_TIMEOUT_SECONDS
      env        = { (local.model_env) = local.names.MODEL_CURRENT }
      layers     = []
      log_format = "Text"
      log_level  = null
    }
    cheaper = {
      name    = local.names.ANSWER_CHEAPER_FUNCTION
      zip     = local.zips.answer
      handler = local.answer_handler
      timeout = local.names.ANSWER_TIMEOUT_SECONDS
      # The module's extras first, so they can never replace the model.
      env        = merge(var.cheaper_extra_env, { (local.model_env) = local.names.MODEL_CHEAPER })
      layers     = var.cheaper_extra_layers
      log_format = "Text"
      log_level  = null
    }
    router = {
      name       = local.names.ROUTER_FUNCTION
      zip        = local.zips.router
      handler    = local.router_handler
      timeout    = local.names.ROUTER_TIMEOUT_SECONDS
      env        = {}
      layers     = []
      log_format = local.sdk_log_format
      log_level  = local.sdk_log_level
    }
    judge = {
      name       = local.names.JUDGE_FUNCTION
      zip        = local.zips.judge
      handler    = local.judge_handler
      timeout    = local.names.JUDGE_TIMEOUT_SECONDS
      env        = {}
      layers     = []
      log_format = local.sdk_log_format
      log_level  = local.sdk_log_level
    }
  }

  # The functions that call Bedrock, each with the one inference profile it calls.
  bedrock_callers = {
    current = local.names.MODEL_CURRENT
    cheaper = local.names.MODEL_CHEAPER
    judge   = local.names.MODEL_JUDGE
  }

  # Converse runs in the account's default Bedrock project unless a request names one, and
  # needs bedrock:InvokeModel on that project too (Bedrock API reference, Converse; task
  # 1.2's spike role had it).
  default_project_arn = "arn:aws:bedrock:${local.region}:${local.account_id}:project/default"

  # Built from the names rather than read from the resources, so every policy is known, and
  # shown in full, at plan time.
  function_arns = {
    for key, fn in local.functions :
    key => "arn:aws:lambda:${local.region}:${local.account_id}:function:${fn.name}"
  }
  log_group_arns = {
    for key, fn in local.functions :
    key => "arn:aws:logs:${local.region}:${local.account_id}:log-group:/aws/lambda/${fn.name}"
  }
  runs_table_arn = "arn:aws:dynamodb:${local.region}:${local.account_id}:table/${local.names.RUNS_TABLE}"

  # The route's path, from API_ROUTE_KEY ("POST /ask").
  route_path = split(" ", local.names.API_ROUTE_KEY)[1]
}
