"""pytest settings for agent/.

agent/spike/contracts is a symlink to the repo's contracts/ package, so the
AgentCore CLI copies contracts into the runtime's CodeZip (its packager follows
symlinks). pytest would follow it too and run the contracts tests twice.
"""

collect_ignore = ["spike/contracts"]
