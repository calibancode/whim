"""Host-enforced permissions: grants, approval requests, and the human's decisions.

Scopes are concrete and bound to an implementation:
  plugin.run        run this exact plugin version's code (sandboxed, nothing extra)
  net               network access for this plugin version
  workspace.read    read the character's workspace from this plugin version
  workspace.write   read and write it

A grant applies to one implementation identity (plugin name + content hash), so a new
version needs its own approval. The only standing grant is `plugin.run` for any plugin
(subject "*"), which the human can choose to give when approving.

Only the designated human, authenticated through the host's control interface, can
decide an approval request. Nothing the character does with tools, and nothing anyone
writes in conversation, reaches these functions.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from whim.ledger import dumps, new_id

if TYPE_CHECKING:
    from whim.harness import Harness

SCOPES = ("plugin.run", "net", "workspace.read", "workspace.write")
STANDING = ("plugin.run",)

SCHEMA = """
CREATE TABLE IF NOT EXISTS grants(
  id TEXT PRIMARY KEY,
  subject TEXT NOT NULL,             -- an implementation id, or '*' for a standing grant
  scope TEXT NOT NULL,
  approval_id TEXT,
  granted_by TEXT NOT NULL,          -- principal id
  created_at TEXT NOT NULL,
  revoked_at TEXT
);
CREATE TABLE IF NOT EXISTS approvals(
  id TEXT PRIMARY KEY,
  status TEXT NOT NULL,              -- pending | approved | denied | cancelled
  subject TEXT NOT NULL,             -- implementation id
  scopes TEXT NOT NULL,
  summary TEXT NOT NULL,
  run_id TEXT,
  created_at TEXT NOT NULL,
  decided_at TEXT,
  decided_by TEXT,
  standing TEXT
);
"""


class AuthzError(Exception):
    pass


def ensure_schema(led) -> None:
    led.db.executescript(SCHEMA)


def granted(h: "Harness", subject: str, scope: str) -> bool:
    row = h.ledger.q1(
        "SELECT 1 FROM grants WHERE scope = ? AND revoked_at IS NULL AND (subject = ? OR (subject = '*' AND scope IN "
        f"({','.join('?' * len(STANDING))})))", scope, subject, *STANDING)
    return row is not None


def missing(h: "Harness", subject: str, scopes: list[str]) -> list[str]:
    return [s for s in scopes if not granted(h, subject, s)]


def request(h: "Harness", subject: str, scopes: list[str], summary: str, run_id: str | None) -> dict:
    """Open (or reuse) a pending approval request. Call inside a transaction."""
    led = h.ledger
    existing = led.q1("SELECT * FROM approvals WHERE status = 'pending' AND subject = ? AND scopes = ?",
                      subject, dumps(sorted(scopes)))
    if existing is not None:
        return dict(existing)
    aid = new_id("apr")
    led.x("INSERT INTO approvals(id, status, subject, scopes, summary, run_id, created_at) "
          "VALUES(?, 'pending', ?, ?, ?, ?, ?)", aid, subject, dumps(sorted(scopes)), summary, run_id, h.time.iso())
    led.append("approval.requested", {"approval_id": aid, "subject": subject, "scopes": sorted(scopes),
                                      "summary": summary}, run_id=run_id, actor="character", audience="human")
    return dict(led.q1("SELECT * FROM approvals WHERE id = ?", aid))


def decide(h: "Harness", approval_id: str, approve: bool, principal_id: str, *, standing: bool = False) -> dict:
    """The designated human's decision. Only reachable from the authenticated control
    interface; the caller has already verified the principal. Call inside a transaction."""
    led = h.ledger
    if principal_id != h.cfg.principal_id:
        raise AuthzError("only the designated human can decide approval requests")
    row = led.q1("SELECT * FROM approvals WHERE id = ?", approval_id)
    if row is None:
        raise AuthzError(f"no approval request {approval_id!r}")
    if row["status"] != "pending":
        raise AuthzError(f"{approval_id} is already {row['status']}")
    now = h.time.iso()
    scopes = json.loads(row["scopes"])
    status = "approved" if approve else "denied"
    standing_scopes = [s for s in scopes if s in STANDING] if (approve and standing) else []
    led.x("UPDATE approvals SET status = ?, decided_at = ?, decided_by = ?, standing = ? WHERE id = ?",
          status, now, principal_id, dumps(standing_scopes), approval_id)
    if approve:
        for s in scopes:
            led.x("INSERT INTO grants(id, subject, scope, approval_id, granted_by, created_at) VALUES(?, ?, ?, ?, ?, ?)",
                  new_id("grt"), row["subject"], s, approval_id, principal_id, now)
        for s in standing_scopes:
            led.x("INSERT INTO grants(id, subject, scope, approval_id, granted_by, created_at) VALUES(?, '*', ?, ?, ?, ?)",
                  new_id("grt"), s, approval_id, principal_id, now)
    led.append("approval.decided", {"approval_id": approval_id, "decision": status, "subject": row["subject"],
                                    "scopes": scopes, "standing": standing_scopes}, actor="human", audience="human",
               origin={"kind": "control", "principal_id": principal_id})
    return dict(led.q1("SELECT * FROM approvals WHERE id = ?", approval_id))


def revoke(h: "Harness", grant_id: str, principal_id: str) -> dict:
    led = h.ledger
    if principal_id != h.cfg.principal_id:
        raise AuthzError("only the designated human can revoke grants")
    row = led.q1("SELECT * FROM grants WHERE id = ? AND revoked_at IS NULL", grant_id)
    if row is None:
        raise AuthzError(f"no active grant {grant_id!r}")
    led.x("UPDATE grants SET revoked_at = ? WHERE id = ?", h.time.iso(), grant_id)
    led.append("grant.revoked", {"grant_id": grant_id, "subject": row["subject"], "scope": row["scope"]},
               actor="human", audience="human", origin={"kind": "control", "principal_id": principal_id})
    return dict(row)


def listing(h: "Harness") -> dict:
    led = h.ledger
    return {
        "pending": [dict(r) for r in led.q("SELECT * FROM approvals WHERE status = 'pending' ORDER BY created_at")],
        "grants": [dict(r) for r in led.q("SELECT * FROM grants WHERE revoked_at IS NULL ORDER BY created_at")],
    }
