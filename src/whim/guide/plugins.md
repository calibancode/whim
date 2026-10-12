# Plugins

Source: `<package>/plugins.py`, `<package>/sandbox.py`, helper `<package>/plugin_sdk/`.
tool_describe plugin_manage has the full how-to. In short: a folder in your workspace
with plugin.json and a Python entry file; plugin_manage install, then activate (your
human approves running new code). Plugins run sandboxed, with their own /data folder,
and only the permissions they declare and your human grants (net, workspace.read,
workspace.write). They do not get the folders host_access grants you.
