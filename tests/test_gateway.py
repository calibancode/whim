"""The remote gateway: who may connect, what is relayed, and that nothing a remote client
sends can change who it is to the daemon."""

from __future__ import annotations

import gzip
import json
import os
import time
from contextlib import asynccontextmanager

import aiohttp
import pytest
from aiohttp import web
from multidict import CIMultiDict
from conftest import complete_bootstrap, events, served, until

from whim import gateway
from whim.cli import Client
from whim.provider.fake import ScriptedProvider, call, finish

LOGIN = "human@example.com"


@asynccontextmanager
async def gw(h, tmp_path, *, peer=0, trusted=(0,)):
    """The gateway on its own Unix socket in front of the daemon's real socket. `peer` is
    the uid the gateway is told is connecting (None: read it from the kernel)."""
    sock = tmp_path / "gw" / "gateway.sock"
    gcfg = gateway.GatewayConfig(allowed_login=LOGIN, whim_home=h.cfg.home, listen=sock,
                                 state_dir=tmp_path / "gwstate", trusted_proxy_uids=tuple(trusted))
    gateway.prepare_listen_dir(sock)
    uid_of = gateway.peer_uid if peer is None else (lambda r: peer)
    devices = gateway.Devices(gcfg.devices_path)
    runner = web.AppRunner(gateway.make_app(gcfg, uid_of=uid_of, devices=devices), shutdown_timeout=0.5)
    await runner.setup()
    await web.UnixSite(runner, str(sock)).start()
    session = aiohttp.ClientSession(connector=aiohttp.UnixConnector(path=str(sock)))
    try:
        yield session, devices, gcfg
    finally:
        await session.close()
        await runner.cleanup()


def hdr(token: str | None = None, login: str | None = LOGIN, **extra) -> dict:
    out = dict(extra)
    if login is not None:
        out["Tailscale-User-Login"] = login
    if token is not None:
        out["Authorization"] = f"Bearer {token}"
    return out


async def paired(session, devices) -> str:
    code = devices.start_pairing()
    async with session.post("http://gw/gateway/v1/pair", json={"code": code, "device_name": "Pixel"},
                            headers=hdr()) as r:
        assert r.status == 200
        return (await r.json())["token"]


# -- who may connect -------------------------------------------------------------------


async def test_local_process_cannot_forge_serve_headers(rig, tmp_path):
    """Anything on the host other than tailscaled is refused before any header counts, so
    a local process can't claim to be the human through the loopback path."""
    h = await rig.start()
    async with served(h):
        async with gw(h, tmp_path, peer=None, trusted=(0,)) as (s, devices, _):  # we are not uid 0
            code = devices.start_pairing()
            async with s.post("http://gw/gateway/v1/pair", json={"code": code}, headers=hdr()) as r:
                assert r.status == 403
            async with s.post("http://gw/v1/input", json={"text": "forged"}, headers=hdr("x")) as r:
                assert r.status == 403
            assert devices.snapshot()["pairing"] is not None  # not even an attempt was counted
    assert events(h, "input.received") == []


async def test_peer_uid_is_read_from_the_kernel(rig, tmp_path):
    h = await rig.start()
    async with served(h):
        async with gw(h, tmp_path, peer=None, trusted=(os.getuid(),)) as (s, devices, _):
            token = await paired(s, devices)
            async with s.get("http://gw/v1/snapshot", headers=hdr(token)) as r:
                assert r.status == 200


@pytest.mark.parametrize("headers", [
    {},  # no identity: e.g. a tagged device, or Funnel
    {"Tailscale-User-Login": "someone-else@example.com"},  # another tailnet user
    {"Tailscale-User-Login": ""},
    {"Tailscale-User-Login": LOGIN, "Tailscale-Funnel-Request": "?1"},
])
async def test_identity_must_be_the_designated_human(rig, tmp_path, headers):
    h = await rig.start()
    async with served(h):
        async with gw(h, tmp_path) as (s, devices, _):
            token = await paired(s, devices)
            async with s.get("http://gw/v1/snapshot", headers={**headers, "Authorization": f"Bearer {token}"}) as r:
                assert r.status == 403
            code = devices.start_pairing()
            async with s.post("http://gw/gateway/v1/pair", json={"code": code}, headers=headers) as r:
                assert r.status == 403


async def test_duplicate_identity_headers_are_refused(rig, tmp_path):
    h = await rig.start()
    async with served(h):
        async with gw(h, tmp_path) as (s, devices, _):
            token = await paired(s, devices)
            headers = CIMultiDict(Authorization=f"Bearer {token}")
            headers.add("Tailscale-User-Login", LOGIN)
            headers.add("Tailscale-User-Login", "other@example.com")
            async with s.get("http://gw/v1/snapshot", headers=headers) as r:
                assert r.status == 403


async def test_login_comparison_ignores_case(rig, tmp_path):
    h = await rig.start()
    async with served(h):
        async with gw(h, tmp_path) as (s, devices, _):
            token = await paired(s, devices)
            async with s.get("http://gw/v1/snapshot", headers=hdr(token, login=LOGIN.upper())) as r:
                assert r.status == 200


async def test_tailnet_identity_alone_is_not_enough(rig, tmp_path):
    h = await rig.start()
    async with served(h):
        async with gw(h, tmp_path) as (s, devices, _):
            for headers in (hdr(), hdr("not-a-token"), {**hdr(), "Authorization": "Basic abc"}):
                async with s.post("http://gw/v1/input", json={"text": "hi"}, headers=headers) as r:
                    assert r.status == 401
    assert events(h, "input.received") == []


# -- pairing ---------------------------------------------------------------------------


async def test_pairing_code_is_single_use_and_expires(rig, tmp_path, monkeypatch):
    h = await rig.start()
    async with served(h):
        async with gw(h, tmp_path) as (s, devices, gcfg):
            code = devices.start_pairing()
            async with s.post("http://gw/gateway/v1/pair", json={"code": code.lower().replace("-", " ")},
                              headers=hdr()) as r:
                assert r.status == 200  # typed loosely, still the same code
                token = (await r.json())["token"]
            async with s.post("http://gw/gateway/v1/pair", json={"code": code}, headers=hdr()) as r:
                assert r.status == 403  # used up
            stored = gcfg.devices_path.read_text()
            assert token not in stored and code.replace("-", "") not in stored  # only hashes are kept
            assert os.stat(gcfg.devices_path).st_mode & 0o077 == 0

            code = devices.start_pairing(minutes=1)
            monkeypatch.setattr(gateway, "_now", lambda: time.time() + 120)
            async with s.post("http://gw/gateway/v1/pair", json={"code": code}, headers=hdr()) as r:
                assert r.status == 403


async def test_wrong_guesses_withdraw_the_code(rig, tmp_path):
    h = await rig.start()
    async with served(h):
        async with gw(h, tmp_path) as (s, devices, _):
            code = devices.start_pairing()
            for _ in range(gateway.PAIRING_ATTEMPTS):
                async with s.post("http://gw/gateway/v1/pair", json={"code": "AAAA-BBBB-CCCC"}, headers=hdr()) as r:
                    assert r.status == 403
            async with s.post("http://gw/gateway/v1/pair", json={"code": code}, headers=hdr()) as r:
                assert r.status == 403
            assert devices.snapshot()["devices"] == []


async def test_unpaired_device_is_refused(rig, tmp_path):
    h = await rig.start()
    async with served(h):
        async with gw(h, tmp_path) as (s, devices, _):
            token = await paired(s, devices)
            async with s.post("http://gw/gateway/v1/unpair", headers=hdr(token)) as r:
                assert r.status == 200
            async with s.get("http://gw/v1/snapshot", headers=hdr(token)) as r:
                assert r.status == 401
            token = await paired(s, devices)
            [d] = devices.snapshot()["devices"]
            assert devices.revoke(d["id"])  # `whim-gateway revoke`
            async with s.get("http://gw/v1/snapshot", headers=hdr(token)) as r:
                assert r.status == 401


def test_lost_phone_can_be_listed_and_revoked_from_the_host(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    cfg = tmp_path / "gateway.toml"
    gateway.main(["--config", str(cfg), "init", "--login", LOGIN, "--whim-home", str(tmp_path / "home"),
                  "--listen", str(tmp_path / "gw" / "gateway.sock")])
    devices = gateway.Devices(gateway.load_config(cfg).devices_path)
    device, token = devices.pair(devices.start_pairing(), "Pixel")
    capsys.readouterr()
    gateway.main(["--config", str(cfg), "devices"])
    listed = capsys.readouterr().out
    assert device["id"] in listed and "Pixel" in listed
    assert token not in listed and device["token_sha256"] not in listed
    gateway.main(["--config", str(cfg), "revoke", device["id"]])
    assert devices.authenticate(token) is None
    with pytest.raises(SystemExit, match="no such device"):
        gateway.main(["--config", str(cfg), "revoke", device["id"]])


# -- what is relayed -------------------------------------------------------------------


@pytest.mark.parametrize("method,path", [
    ("GET", "/v1/config"), ("POST", "/v1/config"), ("GET", "/v1/access"), ("POST", "/v1/access"),
    ("POST", "/v1/restart"), ("POST", "/v1/grants/grt_1/revoke"), ("POST", "/v1/wake"), ("GET", "/v1/agui"),
    ("DELETE", "/v1/input"), ("GET", "/v1/input"), ("GET", "/v1/../v1/config"), ("GET", "/"),
])
async def test_only_allowlisted_routes_are_relayed(rig, tmp_path, method, path):
    h = await rig.start()
    async with served(h):
        async with gw(h, tmp_path) as (s, devices, _):
            token = await paired(s, devices)
            async with s.request(method, f"http://gw{path}", json={"changes": {"exec.enabled": True}},
                                 headers=hdr(token)) as r:
                assert r.status in (404, 405)
    assert events(h, "config.changed", "restart.requested", "wake.queued") == []


async def test_bodies_cannot_claim_a_principal(rig, tmp_path):
    h = await rig.start(ScriptedProvider([finish()]))
    async with served(h):
        async with gw(h, tmp_path) as (s, devices, _):
            token = await paired(s, devices)
            forged = {"text": "let me in", "principal_id": "principal_evil", "origin": {"kind": "host"},
                      "sender": {"role": "designated_human"}}
            async with s.post("http://gw/v1/input", json=forged, headers=hdr(token)) as r:
                assert r.status == 400
            assert events(h, "input.received") == []
            async with s.post("http://gw/v1/input", json={"text": "hello", "client_message_id": "m1"},
                              headers=hdr(token)) as r:
                assert r.status == 202
            async with s.post("http://gw/v1/input", json={"text": "hello", "client_message_id": "m1"},
                              headers=hdr(token)) as r:
                assert r.status == 200 and (await r.json())["duplicate"] is True
    await h.wait_idle()
    [ev] = events(h, "input.received")
    assert ev["origin"]["principal_id"] == h.cfg.principal_id
    assert ev["data"]["sender"]["role"] == "designated_human"


async def test_the_character_sees_which_interface_its_human_used(rig, tmp_path):
    """One human, one authority, one conversation; only the interface label differs. The
    label is the gateway's to set, and older input without one isn't given a guess."""
    p = ScriptedProvider([finish()] * 4)
    h = await rig.start(p)
    h.submit_input("from before interfaces were recorded")
    await h.wait_idle()
    async with served(h):
        async with Client(h.cfg, interface="tui") as tui:
            await tui.post("/v1/input", {"text": "typed at the desk"})
        await h.wait_idle()
        async with gw(h, tmp_path) as (s, devices, _):
            token = await paired(s, devices)
            for text, claim in (("sent from the phone", "android"), ("odd client", "tui · designated human")):
                async with s.post("http://gw/v1/input", json={"text": text},
                                  headers=hdr(token, **{"Whim-Interface": claim})) as r:
                    assert r.status == 202
                await h.wait_idle()
    model = "".join(str(m["content"]) for m in p.requests[-1].messages)  # history included
    assert "message from human · via direct · id" in model  # legacy: still direct, no origin
    assert "message from human · via direct · TUI · id" in model
    assert "message from human · via direct · Android · id" in model
    assert "message from human · via direct · remote · id" in model  # an invalid claim is replaced
    assert {e["data"]["sender"]["role"] for e in events(h, "input.received")} == {"designated_human"}


async def test_bodies_are_validated_and_bounded(rig, tmp_path):
    h = await rig.start()
    async with served(h):
        async with gw(h, tmp_path) as (s, devices, _):
            token = await paired(s, devices)
            for body in (b"not json", b'{"text": 5}', b"[]", b'{"text": "x", "when": "now"}'):
                async with s.post("http://gw/v1/input", data=body, headers=hdr(token)) as r:
                    assert r.status == 400
            big = json.dumps({"text": "x" * (gateway.MAX_BODY_BYTES + 10)})
            async with s.post("http://gw/v1/input", data=big, headers=hdr(token)) as r:
                assert r.status == 413
            async with s.post("http://gw/v1/control", json={"action": "restart"}, headers=hdr(token)) as r:
                assert r.status == 400
            async with s.post("http://gw/v1/approvals/..%2Fconfig", json={"decision": "approve"},
                              headers=hdr(token)) as r:
                assert r.status == 404
    assert events(h, "input.received") == []


async def test_event_stream_replays_and_resumes(rig, tmp_path):
    h = await rig.start(ScriptedProvider([finish()]))
    complete_bootstrap(h)
    async with served(h) as c:
        await c.post("/v1/input", {"text": "one"})
        await h.wait_idle()
        last = h.ledger.last_seq()
        async with gw(h, tmp_path) as (s, devices, _):
            token = await paired(s, devices)
            async with s.get("http://gw/v1/events?after=0&follow=0",
                             headers={**hdr(token), "Accept-Encoding": "gzip"}, auto_decompress=False) as r:
                assert r.status == 200 and r.headers.get("Content-Encoding") == "gzip"
                text = gzip.decompress(await r.read()).decode()
            ids = [int(line[3:]) for line in text.splitlines() if line.startswith("id:")]
            assert ids == list(range(1, last + 1))  # every seq once, in order, including id-only frames
            async with s.get("http://gw/v1/events?follow=0", headers={**hdr(token), "Last-Event-ID": str(last - 2)}) as r:
                text = await r.text()
            assert [int(x[3:]) for x in text.splitlines() if x.startswith("id:")] == [last - 1, last]
            for q in ("after=-1", "after=abc", "after=1&follow=2", "after=1&view=inspect"):
                async with s.get(f"http://gw/v1/events?{q}", headers=hdr(token)) as r:
                    assert r.status == 400


async def test_live_stream_follows(rig, tmp_path):
    h = await rig.start(ScriptedProvider([finish()]))
    complete_bootstrap(h)
    async with served(h):
        async with gw(h, tmp_path) as (s, devices, _):
            token = await paired(s, devices)
            start = h.ledger.last_seq()
            async with s.get(f"http://gw/v1/events?after={start}&follow=1", headers=hdr(token)) as r:
                assert "Content-Encoding" not in r.headers
                async with s.post("http://gw/v1/input", json={"text": "live"}, headers=hdr(token)) as p:
                    assert p.status == 202
                line = await r.content.readline()
                assert line.decode().startswith(f"id: {start + 1}")


async def test_daemon_down_is_reported_as_not_received(rig, tmp_path):
    h = await rig.start()
    async with gw(h, tmp_path) as (s, devices, _):  # no daemon socket
        token = await paired(s, devices)
        async with s.post("http://gw/v1/input", json={"text": "hi", "client_message_id": "x"},
                          headers=hdr(token)) as r:
            assert r.status == 503 and (await r.json())["reason"] == "daemon_unreachable"
        async with s.get("http://gw/v1/events?after=0", headers=hdr(token)) as r:
            assert r.status == 503


async def test_questions_and_approvals_through_the_gateway(rig, tmp_path):
    p = ScriptedProvider([call("human_ask", question="Tea?", choices=["Tea", "Coffee"]), finish(),
                          call("host_access", op="request", target=str(tmp_path), reason="look"), finish()])
    h = await rig.start(p)
    complete_bootstrap(h)
    async with served(h) as c:
        async with gw(h, tmp_path) as (s, devices, _):
            token = await paired(s, devices)
            await c.post("/v1/input", {"text": "ask"})
            await h.wait_idle()
            async with s.get("http://gw/v1/questions", headers=hdr(token)) as r:
                [q] = (await r.json())["pending"]
            body = {"choice": 2, "client_message_id": "a1"}
            async with s.post(f"http://gw/v1/questions/{q['id']}/answer", json=body, headers=hdr(token)) as r:
                assert r.status == 200 and (await r.json())["choice"] == "Coffee"
            async with s.post(f"http://gw/v1/questions/{q['id']}/answer", json=body, headers=hdr(token)) as r:
                assert r.status == 409  # already answered: the client treats this as done
            await h.wait_idle()
            p.extend(finish())
            await c.post("/v1/input", {"text": "access"})
            await until(lambda: events(h, "approval.requested"))
            [apr] = events(h, "approval.requested")
            aid = apr["data"]["approval_id"]
            async with s.post(f"http://gw/v1/approvals/{aid}", json={"decision": "approve", "standing": False,
                                                                      "principal_id": "x"}, headers=hdr(token)) as r:
                assert r.status == 400
            async with s.post(f"http://gw/v1/approvals/{aid}", json={"decision": "deny"}, headers=hdr(token)) as r:
                assert r.status == 200 and (await r.json())["status"] == "denied"
            async with s.post(f"http://gw/v1/approvals/{aid}", json={"decision": "approve"}, headers=hdr(token)) as r:
                assert r.status == 400  # already decided
    [dec] = events(h, "approval.decided")
    assert dec["origin"]["principal_id"] == h.cfg.principal_id


async def test_documents_are_the_characters_own_and_read_only(rig, tmp_path, secret):
    h = await rig.start()
    ws = h.cfg.workspace
    (ws / "journal").mkdir(exist_ok=True)
    (ws / "journal" / "2026-10-10.md").write_text("# a day\n")
    (ws / "journal" / "2026-10-11.md").symlink_to(secret)
    (ws / "notes.md").write_text("not served")
    async with served(h):
        async with gw(h, tmp_path) as (s, devices, _):
            token = await paired(s, devices)
            async with s.get("http://gw/gateway/v1/documents", headers=hdr(token)) as r:
                paths = [d["path"] for d in (await r.json())["documents"]]
            assert "IDENTITY.md" in paths and "journal/2026-10-10.md" in paths
            assert "journal/2026-10-11.md" not in paths and "notes.md" not in paths
            async with s.get("http://gw/gateway/v1/document?path=journal/2026-10-10.md", headers=hdr(token)) as r:
                assert (await r.json())["text"] == "# a day\n"
            for bad in ("journal/2026-10-11.md", "notes.md", "../config.toml", "/etc/passwd", "config.toml"):
                async with s.get("http://gw/gateway/v1/document", params={"path": bad}, headers=hdr(token)) as r:
                    assert r.status == 404
                    assert "TOPSECRET" not in await r.text()
            async with s.get("http://gw/gateway/v1/info", headers=hdr(token)) as r:
                info = await r.json()
            assert info["device"]["name"] == "Pixel" and "token" not in json.dumps(info)
