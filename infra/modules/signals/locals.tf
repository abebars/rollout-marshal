locals {
  # Every name and value comes from contracts/names.json, generated from contracts/names.py.
  names      = jsondecode(file("${path.root}/../contracts/names.json"))
  account_id = local.names.ACCOUNT_ID
  region     = local.names.REGION

  # The repository root, where rm-invoker's code lives.
  repo_root = "${path.root}/.."
}
