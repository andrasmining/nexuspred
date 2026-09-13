"""alpha.83: the Rithmic system dropdown — the gateway's own system list."""
from __future__ import annotations

import pytest

from app import rithmic
from app.tradovate import TradovateError


def _frame(msg) -> bytes:
    body = msg.SerializeToString()
    return len(body).to_bytes(4, byteorder="big", signed=True) + body


class _WS:
    """A scripted gateway socket: records what was sent, plays back frames."""
    def __init__(self, frames):
        self.frames = list(frames)
        self.sent: list[bytes] = []
        self.closed = False

    async def send(self, data):
        self.sent.append(bytes(data))

    async def recv(self):
        return self.frames.pop(0)

    async def close(self):
        self.closed = True


@pytest.fixture
def gateway(monkeypatch):
    from async_rithmic.protocol_buffers import request_rithmic_system_info_pb2 as rq, response_rithmic_system_info_pb2 as rs
    from async_rithmic.protocol_buffers import response_heartbeat_pb2 as hb
    rithmic._systems_cache.clear()
    box = {"sockets": [], "codes": ["0"], "names": ["Rithmic Paper Trading", "TopstepTrader", "Apex", " Apex "]}

    async def connect(url):
        beat = hb.ResponseHeartbeat(); beat.template_id = 19
        resp = rs.ResponseRithmicSystemInfo(); resp.template_id = 17
        resp.rp_code.extend(box["codes"]); resp.system_name.extend(box["names"])
        ws = _WS([b"", _frame(beat), _frame(resp)])
        ws.url = url
        box["sockets"].append(ws)
        return ws
    monkeypatch.setattr(rithmic, "_ws_connect", connect)
    box["request_cls"] = rq.RequestRithmicSystemInfo
    return box


async def test_list_systems_asks_the_gateway_the_way_every_client_must(gateway):
    names = await rithmic.list_systems("chicago")
    assert names == ["Apex", "Rithmic Paper Trading", "TopstepTrader"]              # deduplicated, trimmed, sorted
    ws = gateway["sockets"][0]
    assert ws.url == rithmic.GATEWAYS["chicago"] and ws.closed
    req = gateway["request_cls"](); req.ParseFromString(ws.sent[0][4:])
    assert req.template_id == 16 and int.from_bytes(ws.sent[0][:4], "big") == len(ws.sent[0]) - 4


async def test_list_systems_is_cached_per_gateway_and_refreshable(gateway):
    await rithmic.list_systems("paper")
    await rithmic.list_systems("", environment="demo")                              # the demo default is the paper gateway
    assert len(gateway["sockets"]) == 1
    await rithmic.list_systems("europe")
    assert len(gateway["sockets"]) == 2
    await rithmic.list_systems("paper", fresh=True)
    assert len(gateway["sockets"]) == 3


async def test_list_systems_refuses_a_foreign_host_and_reports_a_gateway_error(gateway):
    with pytest.raises(ValueError):
        await rithmic.list_systems("wss://evil.example/ws")
    gateway["codes"] = ["1", "no system info"]
    with pytest.raises(TradovateError, match="refused the system list"):
        await rithmic.list_systems("test")
    assert gateway["sockets"][-1].closed


async def test_list_systems_reports_an_unreachable_gateway(monkeypatch):
    rithmic._systems_cache.clear()

    async def down(url):
        raise OSError("connection refused")
    monkeypatch.setattr(rithmic, "_ws_connect", down)
    with pytest.raises(TradovateError, match="not reachable"):
        await rithmic.list_systems("chicago")


def test_resolve_gateway():
    assert rithmic.resolve_gateway("", "demo") == rithmic.GATEWAYS["paper"]
    assert rithmic.resolve_gateway("", "live") == rithmic.GATEWAYS["chicago"]
    assert rithmic.resolve_gateway("Europe") == rithmic.GATEWAYS["europe"]
    assert rithmic.resolve_gateway("wss://rprotocol-de.rithmic.com:443") == "wss://rprotocol-de.rithmic.com:443"
    with pytest.raises(ValueError):
        rithmic.resolve_gateway("wss://rithmic.com.evil.example:443")


async def test_systems_api(client, monkeypatch):
    async def fake(gateway, *, environment="demo", fresh=False):
        return ["Apex", "Rithmic Paper Trading"]
    monkeypatch.setattr(rithmic, "list_systems", fake)
    r = await client.get("/api/rithmic/systems?gateway=chicago&environment=live")
    assert r.status_code == 200 and r.json() == {"gateway": rithmic.GATEWAYS["chicago"], "systems": ["Apex", "Rithmic Paper Trading"]}
    assert (await client.get("/api/rithmic/systems?gateway=wss://evil.example")).status_code == 400

    async def down(gateway, *, environment="demo", fresh=False):
        raise TradovateError("Rithmic gateway wss://x not reachable: timeout")
    monkeypatch.setattr(rithmic, "list_systems", down)
    r = await client.get("/api/rithmic/systems")
    assert r.status_code == 502 and "not reachable" in r.json()["detail"]


async def test_systems_api_requires_a_login(admin, anon_client):
    assert (await anon_client.get("/api/rithmic/systems")).status_code in (401, 403)
