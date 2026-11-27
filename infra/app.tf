# Task 3.5: the app module (design.md section 5). It takes no required inputs;
# the FIS module (task 4.3) fills cheaper_extra_layers and cheaper_extra_env in
# infra/fis.tf, which the lead adds with 4.3.
module "app" {
  source = "./modules/app"
}

# The route's full URL, for scripts/load.py --url (task 3.6). Sensitive, so it stays
# out of plan and apply output, which the lead attaches to public pull requests;
# read it with: terraform output -raw ask_url
output "ask_url" {
  value     = module.app.ask_url
  sensitive = true
}
