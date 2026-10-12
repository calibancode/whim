# Configuration

Settings live in the home's config.toml; source: `<package>/config.py` (schema and
defaults, `Config.validate` for cross-field rules) and `<package>/settings.py` (policy).

- host_config get → all settings; get with a key → type, default, when it applies, and
  whether changing it needs approval.
- host_config set {"key": value} → validated against the whole configuration, then saved
  atomically. `live` settings apply at once; `restart` ones after a restart
  (host_restart); sensitive ones (provider, limits, exec, plugins, the compaction model)
  become a request your human approves first.
- Secrets (API keys) are never settings; your human manages them with `whim auth`.
Your human uses the same mechanism: `whim config show|get|set|validate`.
