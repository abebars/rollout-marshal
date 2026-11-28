# Signals module wiring (design.md sections 5, 7 and 8; task 4.1). Owner:
# integration lead (design.md section 12). The threshold comes from
# contracts/names.json; invoker_stub and incident_email come from the
# git-ignored infra/terraform.tfvars.

module "signals" {
  source = "./modules/signals"

  alarm_threshold_ms = local.names.ALARM_THRESHOLD_MS
  invoker_stub       = var.invoker_stub
  incident_email     = var.incident_email
}
