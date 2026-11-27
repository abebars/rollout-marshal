"""pytest settings for agent/.

agent/runtime/contracts (the deployed runtime's) and agent/spike/contracts (task 1.3's) are
symlinks to the repo's contracts/ package, so the AgentCore CLI copies contracts into the
runtime's CodeZip (its packager follows symlinks). pytest would follow them too and run the
contracts tests again.
"""

collect_ignore = ["runtime/contracts", "spike/contracts"]
