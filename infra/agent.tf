# Agent module wiring (design.md sections 7 and 8; tasks 2.2 and 2.7). Owner:
# integration lead (design.md section 12). It rides on 2.2's branch so dev never
# wires a module that is not there yet. The module takes no inputs: it reads
# contracts/names.json itself.

module "agent" {
  source = "./modules/agent"
}
