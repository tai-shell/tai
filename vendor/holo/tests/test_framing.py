import json

import pytest

from holo.framing import (
    PROTOCOL_VERSION,
    Frame,
    FrameError,
    Reassembler,
    chunk,
    decode,
)


def test_frame_roundtrip_basic():
    f = Frame(session="s1", type="cmd", data=b"hello world")
    decoded = decode(f.encode())
    assert decoded.session == "s1"
    assert decoded.type == "cmd"
    assert decoded.data == b"hello world"
    assert decoded.seq == 0
    assert decoded.total == 1
    assert decoded.v == PROTOCOL_VERSION


def test_frame_roundtrip_empty_payload():
    f = Frame(session="s1", type="ack")
    decoded = decode(f.encode())
    assert decoded.data == b""
    assert decoded.type == "ack"


def test_frame_roundtrip_binary_payload():
    payload = bytes(range(256))
    f = Frame(session="s", type="result", data=payload)
    decoded = decode(f.encode())
    assert decoded.data == payload


def test_decode_rejects_bad_json():
    with pytest.raises(FrameError, match="invalid json"):
        decode("not json")


def test_decode_rejects_non_object_json():
    with pytest.raises(FrameError, match="not a json object"):
        decode("[]")


def test_decode_rejects_missing_fields():
    raw = json.dumps({"v": 1, "session": "s", "type": "cmd"})
    with pytest.raises(FrameError, match="missing fields"):
        decode(raw)


def test_decode_rejects_unknown_version():
    f = Frame(session="s", type="cmd", v=99)
    with pytest.raises(FrameError, match="unsupported version"):
        decode(f.encode())


def test_decode_rejects_unknown_type():
    f = Frame(session="s", type="cmd")
    env = json.loads(f.encode())
    env["type"] = "garbage"
    with pytest.raises(FrameError, match="unknown frame type"):
        decode(json.dumps(env))


def test_decode_rejects_crc_mismatch():
    f = Frame(session="s", type="cmd", data=b"hello")
    env = json.loads(f.encode())
    env["crc"] = "00000000"
    with pytest.raises(FrameError, match="crc mismatch"):
        decode(json.dumps(env))


def test_decode_rejects_invalid_base64():
    f = Frame(session="s", type="cmd", data=b"hello")
    env = json.loads(f.encode())
    env["data"] = "!!!not-base64!!!"
    with pytest.raises(FrameError, match="invalid base64"):
        decode(json.dumps(env))


def test_chunk_single_under_limit():
    frames = chunk(b"short", session="s", type="cmd")
    assert len(frames) == 1
    assert frames[0].seq == 0
    assert frames[0].total == 1
    assert frames[0].data == b"short"


def test_chunk_empty_payload():
    frames = chunk(b"", session="s", type="cmd")
    assert len(frames) == 1
    assert frames[0].data == b""
    assert frames[0].total == 1


def test_chunk_multi_share_id():
    payload = b"x" * 1000
    frames = chunk(payload, session="s", type="cmd", max_chunk=300)
    assert len(frames) == 4
    assert all(f.id == frames[0].id for f in frames)
    assert [f.seq for f in frames] == [0, 1, 2, 3]
    assert all(f.total == 4 for f in frames)
    assert b"".join(f.data for f in frames) == payload


def test_chunk_invalid_max():
    with pytest.raises(ValueError, match="max_chunk"):
        chunk(b"x", session="s", type="cmd", max_chunk=0)


def test_reassembler_single_frame():
    r = Reassembler()
    f = Frame(session="s", type="cmd", data=b"hi")
    assert r.feed(f) == b"hi"


def test_reassembler_multi_frame_in_order():
    r = Reassembler()
    frames = chunk(b"x" * 500, session="s", type="cmd", max_chunk=100)
    results = [r.feed(f) for f in frames]
    assert results[:-1] == [None] * (len(frames) - 1)
    assert results[-1] == b"x" * 500


def test_reassembler_multi_frame_out_of_order():
    r = Reassembler()
    frames = chunk(b"abcdefghij", session="s", type="cmd", max_chunk=2)
    results = [r.feed(f) for f in reversed(frames)]
    assert results[-1] == b"abcdefghij"


def test_reassembler_idempotent_replay_after_complete():
    r = Reassembler()
    f = Frame(session="s", type="cmd", data=b"once")
    assert r.feed(f) == b"once"
    assert r.feed(f) is None


def test_reassembler_duplicate_chunk_in_progress():
    r = Reassembler()
    frames = chunk(b"hello world", session="s", type="cmd", max_chunk=3)
    assert r.feed(frames[0]) is None
    assert r.feed(frames[0]) is None
    last = None
    for f in frames[1:]:
        last = r.feed(f)
    assert last == b"hello world"


def test_reassembler_rejects_seq_out_of_range():
    r = Reassembler()
    bad = Frame(session="s", type="cmd", data=b"x", seq=5, total=2)
    with pytest.raises(FrameError, match="out of range"):
        r.feed(bad)


def test_reassembler_rejects_inconsistent_total():
    r = Reassembler()
    f1 = Frame(session="s", type="cmd", data=b"a", seq=0, total=2, id="abc")
    f2 = Frame(session="s", type="cmd", data=b"b", seq=1, total=3, id="abc")
    r.feed(f1)
    with pytest.raises(FrameError, match="inconsistent total"):
        r.feed(f2)


def test_reassembler_independent_messages():
    r = Reassembler()
    f1 = Frame(session="s", type="cmd", data=b"alpha", id="m1")
    f2 = Frame(session="s", type="cmd", data=b"beta", id="m2")
    assert r.feed(f1) == b"alpha"
    assert r.feed(f2) == b"beta"
