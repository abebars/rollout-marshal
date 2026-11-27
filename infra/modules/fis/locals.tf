locals {
  # Every shared name and value comes from contracts/names.json. The path is relative to this
  # module, so it resolves both from infra/ and under terraform test in this directory.
  names      = jsondecode(file("${path.module}/../../../contracts/names.json"))
  account_id = local.names.ACCOUNT_ID
  region     = local.names.REGION

  # Built from names, not from other resources, so the outputs the app module takes do not
  # depend on the app module's outputs.
  config_bucket_arn    = "arn:aws:s3:::${local.names.FIS_CONFIG_BUCKET}"
  config_objects_arn   = "${local.config_bucket_arn}/${local.names.FIS_CONFIG_PREFIX}/*"
  cheaper_function_arn = "arn:aws:lambda:${local.region}:${local.account_id}:function:${var.cheaper_function_name}"
}
