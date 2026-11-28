# Exactly the three inputs the lead's root wiring (infra/signals.tf) passes.

variable "alarm_threshold_ms" {
  description = "Threshold of alarm rm-cheaper-latency-p95 in milliseconds (design.md section 5). The root fills it from contracts/names.json; a recalibration is a contract change."
  type        = number
}

variable "invoker_stub" {
  description = "When true, rm-invoker gets env STUB=1 and only logs the payload it would send (design.md section 7)."
  type        = bool
}

variable "incident_email" {
  description = "The one email address subscribed to SNS topic rm-incidents (design.md section 8)."
  type        = string
  sensitive   = true
}
