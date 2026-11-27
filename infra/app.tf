# Task 3.5: the app module (design.md section 5). It takes no required inputs;
# the FIS module (task 4.3) fills cheaper_extra_layers and cheaper_extra_env in
# infra/fis.tf, which the lead adds with 4.3.
module "app" {
  source = "./modules/app"
}
