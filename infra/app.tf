# Task 3.5: the app module (design.md section 5). The FIS module (task 4.3,
# infra/fis.tf) gives rm-answer-cheaper alone its layer and environment
# (design.md section 9); neither of those depends on this module, so no cycle.
module "app" {
  source               = "./modules/app"
  cheaper_extra_layers = module.fis.cheaper_extra_layers
  cheaper_extra_env    = module.fis.cheaper_extra_env
}

# The route's full URL, for scripts/load.py --url (task 3.6). Sensitive, so it stays
# out of plan and apply output, which the lead attaches to public pull requests;
# read it with: terraform output -raw ask_url
output "ask_url" {
  value     = module.app.ask_url
  sensitive = true
}
