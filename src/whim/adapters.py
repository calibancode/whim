"""The seam for external sources and sinks (chat platforms, games, other agents).

An adapter is host-installed code that the operator configured; it is never the
designated human. It gets an `AdapterHandle`, which is the whole contract:

- `submit(...)`  an inbound message it has verified with its own platform. The host
                 records it as `adapter:<name>`, keeps the adapter's sender id and
                 channel, and gives it no principal: external senders can talk to the
                 character but never approve anything or act as its human.
- a Destination  registered as `<name>`: the character reaches it with
                 message_send to "<name>:<target>" (or reply_to_source), and the
                 adapter's `deliver` settles it as sent, failed, or uncertain.
- `register_tool` optional tools under the adapter's own namespace (`<name>.<tool>`),
                 joining the catalog at the next request like any other change.

Nothing here touches the run loop, the ledger format or the AG-UI projection: inbound
messages are ordinary attributed inputs and outbound ones ordinary deliveries.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from whim import plugins
from whim.tools.registry import Tool

if TYPE_CHECKING:
    from whim.harness import Harness

NAME_RE = re.compile(r"^[a-z][a-z0-9]{1,23}$")


class AdapterHandle:
    def __init__(self, h: "Harness", name: str):
        if not NAME_RE.match(name) or name in plugins.RESERVED:
            raise ValueError(f"invalid adapter name {name!r}")
        self.h = h
        self.name = name

    @property
    def source_kind(self) -> str:
        return f"adapter:{self.name}"

    def submit(self, *, sender_id: str, sender_name: str, channel: str, text: str, message_id: str) -> tuple[dict, bool]:
        """An inbound message whose sender the adapter verified with its platform."""
        route = {"via": self.name, "label": f"{sender_name} ({self.name})", "destination": f"{self.name}:{channel}",
                 "sender_id": sender_id, "sender_name": sender_name, "channel": channel}
        return self.h.submit_input(text, source_kind=self.source_kind, principal_id=None,
                                   source_message_id=message_id, route=route)

    def register_tool(self, tool: Tool) -> None:
        if not tool.name.startswith(f"{self.name}.") or not tool.impl.startswith(f"adapter:{self.name}."):
            raise ValueError(f"adapter tools must be named {self.name}.<tool> with impl adapter:{self.name}.<...>")
        self.h.registry.register(tool)  # refuses replay-safety claims and name collisions
        with self.h.ledger.tx():
            plugins.commit_generation(self.h, f"adapter {self.name} registered {tool.name}")
