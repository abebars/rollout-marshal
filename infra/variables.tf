variable "invoker_stub" {
  description = "When true, rm-invoker gets env STUB=1 and only logs the payload it would send. The lead sets it to false in task 5.1 (design.md section 7)."
  type        = bool
  default     = true
}
