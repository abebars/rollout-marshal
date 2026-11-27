# The FIS module with a mocked AWS provider: no AWS call. Run from infra/modules/fis:
#   terraform init -backend=false && terraform test
# The layer ARN is the value 4.3 read from the SSM parameter (evidence/4.3-fis.md).

mock_provider "aws" {
  override_data {
    target = data.aws_ssm_parameter.fis_extension
    values = {
      insecure_value = "arn:aws:lambda:us-east-1:211125607513:layer:aws-fis-extension-arm64:441"
    }
  }
}

variables {
  cheaper_function_name   = "rm-answer-cheaper"
  cheaper_role_name       = "rm-answer-cheaper-role"
  config_bucket_name      = "rm-fis-config-818928662830"
  config_prefix           = "FisConfigs"
  experiment_role_name    = "rm-fis-role"
  experiment_policy_name  = "rm-fis-actions"
  config_read_policy_name = "rm-fis-config-read"
}

run "the_outputs_the_app_module_takes" {
  command = plan

  assert {
    condition     = output.cheaper_extra_layers == ["arn:aws:lambda:us-east-1:211125607513:layer:aws-fis-extension-arm64:441"]
    error_message = "cheaper_extra_layers must be exactly the arm64 extension layer read from SSM."
  }

  assert {
    condition = output.cheaper_extra_env == {
      AWS_FIS_CONFIGURATION_LOCATION     = "arn:aws:s3:::rm-fis-config-818928662830/FisConfigs/"
      AWS_LAMBDA_EXEC_WRAPPER            = "/opt/aws-fis/bootstrap"
      AWS_FIS_SLOW_POLL_INTERVAL_SECONDS = "10"
    }
    error_message = "cheaper_extra_env must be the guide's two required variables plus the 10 s slow poll."
  }
}

run "the_layer_parameter_is_the_arm64_one" {
  command = plan

  assert {
    condition     = data.aws_ssm_parameter.fis_extension.name == "/aws/service/fis/lambda-extension/AWS-FIS-extension-arm64/1.x.x"
    error_message = "The layer must come from the arm64 extension's public SSM parameter."
  }
}

run "the_template" {
  command = plan

  assert {
    condition     = aws_fis_experiment_template.delay_cheaper.tags["Name"] == "rm-delay-cheaper"
    error_message = "The template is named rm-delay-cheaper through its Name tag."
  }

  assert {
    condition     = one(aws_fis_experiment_template.delay_cheaper.action).action_id == "aws:lambda:invocation-add-delay"
    error_message = "The template's one action is aws:lambda:invocation-add-delay."
  }

  assert {
    condition     = aws_fis_experiment_template.delay_cheaper.description == "Adds 10000 ms to 100 percent of rm-answer-cheaper's invocations for 6 minutes."
    error_message = "The description names the targeted function, from cheaper_function_name."
  }

  assert {
    condition = {
      for p in one(aws_fis_experiment_template.delay_cheaper.action).parameter : p.key => p.value
      } == {
      duration                 = "PT6M"
      invocationPercentage     = "100"
      startupDelayMilliseconds = "10000"
    }
    error_message = "The action adds 10000 ms to 100 percent of invocations for 6 minutes."
  }

  assert {
    condition     = one(one(aws_fis_experiment_template.delay_cheaper.action).target).key == "Functions"
    error_message = "The action's target key is Functions (fis get-action)."
  }

  assert {
    condition     = one(aws_fis_experiment_template.delay_cheaper.target).resource_type == "aws:lambda:function"
    error_message = "The target is a Lambda function."
  }

  assert {
    condition     = toset(one(aws_fis_experiment_template.delay_cheaper.target).resource_arns) == toset(["arn:aws:lambda:us-east-1:818928662830:function:rm-answer-cheaper"])
    error_message = "The target is rm-answer-cheaper and nothing else."
  }

  assert {
    condition     = one(aws_fis_experiment_template.delay_cheaper.target).selection_mode == "ALL"
    error_message = "Every targeted function is selected."
  }

  assert {
    condition     = one(aws_fis_experiment_template.delay_cheaper.stop_condition).source == "none"
    error_message = "The fault ends by its duration, not a stop condition."
  }
}

run "the_experiment_role" {
  command = plan

  assert {
    condition     = aws_iam_role.experiment.name == "rm-fis-role"
    error_message = "The experiment role is rm-fis-role."
  }

  assert {
    condition = jsondecode(aws_iam_role.experiment.assume_role_policy).Statement[0] == {
      Effect    = "Allow"
      Principal = { Service = "fis.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        StringEquals = { "aws:SourceAccount" = "818928662830" }
        ArnLike      = { "aws:SourceArn" = "arn:aws:fis:us-east-1:818928662830:experiment/*" }
      }
    }
    error_message = "Only FIS experiments in this account may assume the role."
  }

  assert {
    condition     = aws_iam_role_policy.experiment.name == "rm-fis-actions"
    error_message = "The experiment role's inline policy is rm-fis-actions."
  }

  assert {
    condition = jsondecode(aws_iam_role_policy.experiment.policy).Statement == [
      {
        Sid      = "AllowFisToWriteAndDeleteFaultConfigurations"
        Effect   = "Allow"
        Action   = ["s3:PutObject", "s3:DeleteObject"]
        Resource = "arn:aws:s3:::rm-fis-config-818928662830/FisConfigs/*"
      },
      {
        Sid      = "AllowFisToInspectLambdaFunctions"
        Effect   = "Allow"
        Action   = ["lambda:GetFunction"]
        Resource = "*"
      },
      {
        Sid      = "AllowFisToDoTagLookups"
        Effect   = "Allow"
        Action   = ["tag:GetResources"]
        Resource = "*"
      },
    ]
    error_message = "rm-fis-actions is the FIS Lambda actions guide's experiment policy, on rm-fis-config's folder."
  }
}

run "the_cheaper_function_reads_only_the_configuration_folder" {
  command = plan

  assert {
    condition     = aws_iam_role_policy.config_read.name == "rm-fis-config-read" && aws_iam_role_policy.config_read.role == "rm-answer-cheaper-role"
    error_message = "rm-fis-config-read goes on the cheaper function's role, by name."
  }

  assert {
    condition = jsondecode(aws_iam_role_policy.config_read.policy).Statement == [
      {
        Sid       = "AllowListingConfigLocation"
        Effect    = "Allow"
        Action    = ["s3:ListBucket"]
        Resource  = ["arn:aws:s3:::rm-fis-config-818928662830"]
        Condition = { StringLike = { "s3:prefix" = ["FisConfigs/*"] } }
      },
      {
        Sid      = "AllowReadingObjectFromConfigLocation"
        Effect   = "Allow"
        Action   = "s3:GetObject"
        Resource = ["arn:aws:s3:::rm-fis-config-818928662830/FisConfigs/*"]
      },
    ]
    error_message = "rm-fis-config-read is the guide's extension read policy, on the configuration folder only."
  }
}

run "the_configuration_bucket" {
  command = plan

  assert {
    condition     = aws_s3_bucket.config.bucket == "rm-fis-config-818928662830"
    error_message = "The configuration bucket is rm-fis-config-818928662830."
  }

  assert {
    condition = (
      aws_s3_bucket_public_access_block.config.block_public_acls
      && aws_s3_bucket_public_access_block.config.block_public_policy
      && aws_s3_bucket_public_access_block.config.ignore_public_acls
      && aws_s3_bucket_public_access_block.config.restrict_public_buckets
    )
    error_message = "The configuration bucket blocks every kind of public access."
  }
}
