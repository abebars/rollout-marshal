# What the root passes into the app module's inputs of the same names (task 3.5). Neither
# depends on the app module's outputs: the layer comes from SSM, and the variables are built
# from names. That keeps the two modules free of a cycle.

output "cheaper_extra_layers" {
  description = "The AWS FIS extension's arm64 layer ARN, for rm-answer-cheaper."
  value       = [data.aws_ssm_parameter.fis_extension.insecure_value]
}

output "cheaper_extra_env" {
  description = "The FIS guide's environment variables for rm-answer-cheaper, with the 10 s slow poll from design.md section 9."
  value = {
    AWS_FIS_CONFIGURATION_LOCATION  = "${local.config_bucket_arn}/${var.config_prefix}/"
    AWS_LAMBDA_EXEC_WRAPPER         = "/opt/aws-fis/bootstrap"
    (local.names.FIS_SLOW_POLL_ENV) = tostring(local.names.FIS_SLOW_POLL_SECONDS)
  }
}

output "experiment_template_id" {
  description = "The ID of rm-delay-cheaper, which scripts/inject_fault.sh also finds by its Name tag."
  value       = aws_fis_experiment_template.delay_cheaper.id
}
