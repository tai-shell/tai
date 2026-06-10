"""Tests for the loopback WS server.

Spins up a real websockets server on a random loopback port, drives it
with the websockets sync client. Exercises:

- handshake validation (sid, token, malformed messages)
- routing to the right Channel via the registry
- multi-channel routing in the same process
- result frames flow back through `_on_ws_message`
- detach on socket close
"""

from __future__ import annotations

import json
import time

import pytest
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

from holo import framing
from holo.channel import Channel
from holo.registry import ChannelRegistry
from holo.ws_server import WSServer


@pytest.fixture
def server():
    registry = ChannelRegistry()
    s = WSServer(registry)
    s.start()
    try:
        yield s, registry
    finally:
        s.stop()


def _wait(predicate, timeout=2.0, step=0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return False


def _make_channel(sid="sess-1"):
    ch = Channel()
    ch.session = sid
    ch._window_id = 1
    return ch


class TestHandshake:
    def test_attaches_channel_on_valid_handshake(self, server):
        s, registry = server
        ch = _make_channel("sid-x")
        registry.register("sid-x", ch)

        with connect(s.url) as ws:
            ws.send(json.dumps({"type": "handshake", "sid": "sid-x", "token": s.token}))
            ack = json.loads(ws.recv(timeout=1.0))
            assert ack == {"type": "handshake_ack"}
            assert _wait(lambda: ch._ws_ready), "channel not attached"

    def test_rejects_unknown_sid(self, server):
        s, _ = server
        with connect(s.url) as ws:
            ws.send(json.dumps({"type": "handshake", "sid": "ghost", "token": s.token}))
            with pytest.raises(ConnectionClosed):
                ws.recv(timeout=1.0)

    def test_rejects_bad_token(self, server):
        s, registry = server
        ch = _make_channel()
        registry.register("sess-1", ch)
        with connect(s.url) as ws:
            ws.send(json.dumps({"type": "handshake", "sid": "sess-1", "token": "wrong"}))
            with pytest.raises(ConnectionClosed):
                ws.recv(timeout=1.0)
        assert not ch._ws_ready

    def test_rejects_non_handshake_first_message(self, server):
        s, registry = server
        ch = _make_channel()
        registry.register("sess-1", ch)
        with connect(s.url) as ws:
            ws.send(json.dumps({"type": "cmd", "frame": "{}"}))
            with pytest.raises(ConnectionClosed):
                ws.recv(timeout=1.0)

    def test_rejects_bad_json(self, server):
        s, _ = server
        with connect(s.url) as ws:
            ws.send("not json at all")
            with pytest.raises(ConnectionClosed):
                ws.recv(timeout=1.0)


class TestResultRouting:
    def test_result_frame_lands_on_pending_queue(self, server):
        s, registry = server
        ch = _make_channel("sid-1")
        registry.register("sid-1", ch)

        with connect(s.url) as ws:
            ws.send(json.dumps({"type": "handshake", "sid": "sid-1", "token": s.token}))
            ws.recv(timeout=1.0)
            assert _wait(lambda: ch._ws_ready)

            # Simulate a pending command waiting for frame id "abc".
            import queue

            q: queue.Queue = queue.Queue(maxsize=1)
            ch._ws_pending["abc"] = q

            reply = framing.Frame(
                session="sid-1",
                type="result",
                data=json.dumps({"value": 42}).encode("utf-8"),
                id="abc",
            )
            ws.send(json.dumps({"type": "result", "frame": reply.encode()}))

            got = q.get(timeout=1.0)
            assert got.id == "abc"
            assert json.loads(got.data.decode("utf-8")) == {"value": 42}

    def test_result_with_mismatched_session_is_dropped(self, server):
        s, registry = server
        ch = _make_channel("sid-1")
        registry.register("sid-1", ch)

        with connect(s.url) as ws:
            ws.send(json.dumps({"type": "handshake", "sid": "sid-1", "token": s.token}))
            ws.recv(timeout=1.0)
            assert _wait(lambda: ch._ws_ready)

            import queue

            q: queue.Queue = queue.Queue(maxsize=1)
            ch._ws_pending["abc"] = q

            reply = framing.Frame(
                session="DIFFERENT",
                type="result",
                data=b"{}",
                id="abc",
            )
            ws.send(json.dumps({"type": "result", "frame": reply.encode()}))

            with pytest.raises(queue.Empty):
                q.get(timeout=0.3)


class TestDetach:
    def test_close_clears_ws_ready(self, server):
        s, registry = server
        ch = _make_channel("sid-1")
        registry.register("sid-1", ch)

        with connect(s.url) as ws:
            ws.send(json.dumps({"type": "handshake", "sid": "sid-1", "token": s.token}))
            ws.recv(timeout=1.0)
            assert _wait(lambda: ch._ws_ready)

        # connection closed; channel should detach
        assert _wait(lambda: not ch._ws_ready)
        assert ch._ws_conn is None


class TestStaticFiles:
    def test_serves_popup_html_on_get(self, server):
        s, _ = server
        import urllib.request

        with urllib.request.urlopen(s.popup_url) as resp:
            assert resp.status == 200
            assert resp.headers.get("Content-Type", "").startswith("text/html")
            body = resp.read().decode("utf-8")
        assert "holo console" in body
        assert "WebSocket" in body

    def test_serves_framing_js_on_get(self, server):
        s, _ = server
        import urllib.request

        with urllib.request.urlopen(f"http://127.0.0.1:{s.port}/framing.js") as resp:
            assert resp.status == 200
            assert resp.headers.get("Content-Type", "").startswith("application/javascript")
            body = resp.read().decode("utf-8")
        assert "encodeFrame" in body
        assert "decodeFrame" in body

    def test_unknown_path_does_not_404_into_ws(self, server):
        s, _ = server
        # Anything other than the static files falls through to the WS
        # upgrade path; a plain HTTP GET should fail to upgrade and the
        # server's behavior is to close. We just confirm it doesn't
        # respond as if /popup.html existed.
        import urllib.error
        import urllib.request

        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(f"http://127.0.0.1:{s.port}/")


class TestMultiChannel:
    def test_two_channels_route_independently(self, server):
        s, registry = server
        ch_a = _make_channel("sid-a")
        ch_b = _make_channel("sid-b")
        registry.register("sid-a", ch_a)
        registry.register("sid-b", ch_b)

        with connect(s.url) as ws_a, connect(s.url) as ws_b:
            ws_a.send(json.dumps({"type": "handshake", "sid": "sid-a", "token": s.token}))
            ws_b.send(json.dumps({"type": "handshake", "sid": "sid-b", "token": s.token}))
            ws_a.recv(timeout=1.0)
            ws_b.recv(timeout=1.0)
            assert _wait(lambda: ch_a._ws_ready and ch_b._ws_ready)

            import queue

            qa: queue.Queue = queue.Queue(maxsize=1)
            qb: queue.Queue = queue.Queue(maxsize=1)
            ch_a._ws_pending["fa"] = qa
            ch_b._ws_pending["fb"] = qb

            ra = framing.Frame(session="sid-a", type="result", data=b'{"a":1}', id="fa")
            rb = framing.Frame(session="sid-b", type="result", data=b'{"b":2}', id="fb")
            ws_a.send(json.dumps({"type": "result", "frame": ra.encode()}))
            ws_b.send(json.dumps({"type": "result", "frame": rb.encode()}))

            assert qa.get(timeout=1.0).id == "fa"
            assert qb.get(timeout=1.0).id == "fb"
