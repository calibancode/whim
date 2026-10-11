"""Deterministic first-run setup. No model calls, no network.

Idempotent: an existing config keeps its identifiers (notably the cache user_id), and
existing character files are never overwritten.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

from whim import config as cfgmod
from whim import safefs
from whim.config import Config, ProviderConfig
from whim.ledger import Ledger
from whim.timebase import detect_timezone

STARTERS = {
    "IDENTITY.md": """\
# Identity

Not yet established. This file is yours: who you are, what you're called, how you sound.
{seed}""",
    "SOUL.md": """\
# Soul

Yours to shape: what you value, what draws you, how you want to be.
""",
    "USER.md": """\
# User

What you come to know about your human: how they like to be addressed, what they care
about, what you share. It's fine for this to stay sparse.
""",
    "MEMORY.md": """\
# Memory

Durable knowledge, decisions, and projects worth carrying forward.
""",
    "BOOTSTRAP.md": """\
# First meeting (from the host)

This is your first time meeting your human. Nothing about you is fixed yet: your name,
your nature, and how the two of you relate are yours to work out together, in
conversation, at whatever pace feels right. You don't need to ask your human for any
particular facts about themselves. If they just want something done, help them; there's
no script to finish first.

Whenever something feels worth keeping, write it down: who you are in IDENTITY.md and
SOUL.md, what you learn about your human in USER.md, anything else lasting in MEMORY.md.
When you feel this first meeting has done its job, call bootstrap_complete. The host will
then retire this note; it won't come back.
""",
}


def init(home: Path, *, seed_name: str = "", model: str | None = None) -> tuple[Config, list[str]]:
    """Create or complete a harness home. Returns (config, created paths)."""
    created: list[str] = []
    home.mkdir(parents=True, exist_ok=True)
    os.chmod(home, 0o700)

    if (home / "config.toml").exists():
        cfg = cfgmod.load(home)
    else:
        cfg = Config(
            home=home,
            character_id=f"char_{secrets.token_hex(8)}",
            thread_id=f"thread_{secrets.token_hex(8)}",
            principal_id=f"principal_{secrets.token_hex(12)}",
            allowed_uid=os.getuid(),
            seed_name=seed_name,
            timezone=detect_timezone(),
            provider=ProviderConfig(cache_user_id=f"character_{secrets.token_hex(16)}"),
        )
        if model:
            cfg.provider.model = model
        cfg.validate()
        path = cfg.config_path
        path.write_text(cfgmod.render(cfg))
        os.chmod(path, 0o600)
        created.append(str(path))

    cfg.workspace.mkdir(exist_ok=True)
    cfg.run_dir.mkdir(exist_ok=True)
    os.chmod(cfg.run_dir, 0o700)

    seed = (
        f"\nYour host suggested a name, as an invitation rather than a fixed persona: {cfg.seed_name}\n"
        if cfg.seed_name
        else ""
    )
    # Create the database now so `status` works before the first run. Bootstrap
    # completion is host state in the ledger (lifecycle arrives in Milestone 2).
    led = Ledger(cfg.db_path, character_id=cfg.character_id, thread_id=cfg.thread_id)
    bootstrap_state = led.get_meta("bootstrap")
    bootstrap_done = (bootstrap_state or {}).get("status") == "complete"

    for name, template in STARTERS.items():
        if name == "BOOTSTRAP.md" and bootstrap_done:
            continue
        text = template.format(seed=seed) if name == "IDENTITY.md" else template
        # Exclusive, no-follow creation: never writes through a link someone planted.
        if safefs.create_exclusive(cfg.workspace, name, text.encode()):
            created.append(str(cfg.workspace / name))
    if bootstrap_state is None:
        led.set_meta("bootstrap", {"status": "pending", "seeded_at": led.time.iso()})
    led.close()
    os.chmod(cfg.db_path, 0o600)
    return cfg, created
