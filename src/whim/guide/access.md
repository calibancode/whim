# Access

Source: `<package>/access.py`. host_access lists what you can use:

- your workspace, read-write;
- your own implementation (the `implementation` path), read-only: read it freely to see
  how you work. Editing it needs explicit permission (host_access request, target
  "self", write true), because edited source becomes how the harness behaves after the
  next restart;
- folders your human granted, read-only or read-write;
- tool folders: granted folders whose programs are also on PATH for shell commands.

However broad a grant, some places stay hidden (the harness home, credentials, /run,
secret stores) unless granted themselves, and what the harness runs from (its source,
Python environment, systemd unit) stays read-only without "self" write access.

File tools take absolute paths inside these folders. Symbolic links are not followed.
To ask for more: host_access op request with target (a folder path, "self", "shell" or
"network") and a reason. Your human decides and sees warnings for risky requests;
you're told either way.
