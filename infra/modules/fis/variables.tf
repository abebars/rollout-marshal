# The cheaper answer function, from the app module's outputs of the same names (design.md
# section 9). Every other name comes from contracts/names.json.
variable "cheaper_function_name" {
  description = "The cheaper answer function the template targets (rm-answer-cheaper)."
  type        = string
}

variable "cheaper_role_name" {
  description = "The cheaper answer function's execution role, which gets rm-fis-config-read."
  type        = string
}
