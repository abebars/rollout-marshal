# What the root passes into the app module's inputs of the same names (design.md section 9).
# Both are built from names only: the pinned layer and the configuration folder. So neither
# depends on the app module's outputs, and the two modules form no cycle.

output "cheaper_extra_layers" {
  description = "The pinned AWS FIS extension arm64 layer, for rm-answer-cheaper."
  value       = [local.names.FIS_EXTENSION_LAYER_ARN]
}

output "cheaper_extra_env" {
  description = "The FIS guide's environment variables for rm-answer-cheaper, with the 10 s slow poll from design.md section 9."
  value = {
    AWS_FIS_CONFIGURATION_LOCATION  = "${local.config_bucket_arn}/${local.names.FIS_CONFIG_PREFIX}/"
    AWS_LAMBDA_EXEC_WRAPPER         = "/opt/aws-fis/bootstrap"
    (local.names.FIS_SLOW_POLL_ENV) = tostring(local.names.FIS_SLOW_POLL_SECONDS)
  }
}

output "experiment_template_id" {
  description = "The ID of rm-delay-cheaper, which scripts/inject_fault.sh also finds by its Name tag."
  value       = aws_fis_experiment_template.delay_cheaper.id
}
