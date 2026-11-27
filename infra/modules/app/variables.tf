# Exactly the two inputs the lead set for task 3.5, both for rm-answer-cheaper only, so the
# FIS module (task 4.3) attaches its extension layer and settings without editing B's files.

variable "cheaper_extra_layers" {
  description = "Layer ARNs added to rm-answer-cheaper, such as the FIS extension (design.md section 9)."
  type        = list(string)
  default     = []
}

variable "cheaper_extra_env" {
  description = "Environment variables added to rm-answer-cheaper (design.md section 9). MODEL_ID always stays the cheaper model's."
  type        = map(string)
  default     = {}
}
