# Task 4.3: the FIS module (design.md section 9). It takes the app module's cheaper
# function and role names, and its outputs go to the app module (infra/app.tf).
module "fis" {
  source                = "./modules/fis"
  cheaper_function_name = module.app.cheaper_function_name
  cheaper_role_name     = module.app.cheaper_role_name
}
