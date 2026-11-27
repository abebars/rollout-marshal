# The cheaper answer function, from the app module's outputs of the same names (task 3.5).
variable "cheaper_function_name" {
  description = "The cheaper answer function the template targets (rm-answer-cheaper)."
  type        = string
}

variable "cheaper_role_name" {
  description = "The cheaper answer function's execution role, which gets rm-fis-config-read."
  type        = string
}

# The lead's names for 4.3, module variables until contract change c4 puts them in
# contracts/names.json; the root then wires them from local.names.
variable "config_bucket_name" {
  description = "The S3 bucket FIS writes fault configurations to (rm-fis-config-818928662830)."
  type        = string
}

variable "config_prefix" {
  description = "The folder in the configuration bucket, without slashes (the FIS guide's example is FisConfigs)."
  type        = string

  validation {
    condition     = can(regex("^[A-Za-z0-9_-]+$", var.config_prefix))
    error_message = "config_prefix is one folder name, without slashes."
  }
}

variable "experiment_role_name" {
  description = "The role FIS assumes to run the experiment (rm-fis-role)."
  type        = string
}

variable "experiment_policy_name" {
  description = "The experiment role's inline policy (rm-fis-actions)."
  type        = string
}

variable "config_read_policy_name" {
  description = "The inline policy on the cheaper function's role that reads the configuration folder (rm-fis-config-read)."
  type        = string
}
