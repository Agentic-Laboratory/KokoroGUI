"""Daemon protocol and dispatch tests.

The engine is stubbed throughout: none of this needs a model, and the suite's
policy is that a default run downloads nothing and touches no audio device.
"""
import asyncio
import json
import os
import shutil
import socket
import tempfile
from pathlib import Path

import pytest

import kokoro_daemon


class StubPlayback:
    """Stands in for the playback module, recording what it was asked to do."""

    def __init__(self):
        self.played = []
        self.stopped = 0

    def play(self, path, blocking=False):
        self.played.append((path, blocking))

    def stop(self):
        self.stopped += 1


@pytest.fixture
def stub_playback(monkeypatch):
    stub = StubPlayback()
    monkeypatch.setattr(kokoro_daemon, "playback", stub)
    return stub


@pytest.fixture
def daemon(monkeypatch, stub_playback):
    """A daemon whose synthesis writes a placeholder file instead of audio."""
    instance = kokoro_daemon.SynthesisDaemon()
    instance.engine = object()

    calls = []

    async def fake_synthesize(text, voice, speed, lang):
        calls.append({"text": text, "voice": voice, "speed": speed, "lang": lang})
        path = Path(os.environ["KOKORO_TEST_TMP"]) / f"line-{len(calls)}.wav"
        path.write_bytes(b"")
        return str(path)

    monkeypatch.setattr(instance, "_synthesize", fake_synthesize)
    instance.calls = calls
    return instance


@pytest.fixture(autouse=True)
def synth_output_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("KOKORO_TEST_TMP", str(tmp_path))
    return tmp_path


@pytest.fixture
def sock_dir():
    """A short-pathed directory for sockets.

    macOS caps a Unix socket path at 104 bytes and pytest's tmp_path names are
    long enough on their own to exceed it, so these cannot live under tmp_path.
    """
    directory = tempfile.mkdtemp(prefix="kd-", dir="/tmp")
    try:
        yield Path(directory)
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_socket_path_prefers_explicit_then_environment(monkeypatch):
    monkeypatch.setenv("KOKORO_TTS_SOCKET", "/tmp/from-env.sock")
    assert kokoro_daemon.socket_path("/tmp/explicit.sock") == "/tmp/explicit.sock"
    assert kokoro_daemon.socket_path() == "/tmp/from-env.sock"
    monkeypatch.delenv("KOKORO_TTS_SOCKET")
    assert kokoro_daemon.socket_path() == str(kokoro_daemon.DAEMON_SOCKET)


def test_speak_synthesizes_then_plays(daemon, stub_playback):
    assert asyncio.run(daemon.speak("A finished line.")) is True
    assert daemon.calls == [
        {
            "text": "A finished line.",
            "voice": kokoro_daemon.DEFAULT_VOICE,
            "speed": kokoro_daemon.DEFAULT_SPEED,
            "lang": kokoro_daemon.DEFAULT_LANG,
        }
    ]
    assert len(stub_playback.played) == 1
    assert stub_playback.played[0][1] is True


def test_speak_overrides_voice_speed_and_language(daemon):
    asyncio.run(daemon.speak("Une ligne.", voice="ff_siwis", speed=1.2, lang="f"))
    assert daemon.calls[0]["voice"] == "ff_siwis"
    assert daemon.calls[0]["speed"] == 1.2
    assert daemon.calls[0]["lang"] == "f"


def test_speak_removes_the_rendered_file(daemon, stub_playback):
    asyncio.run(daemon.speak("Temporary."))
    assert not Path(stub_playback.played[0][0]).exists()


def test_a_newer_request_discards_the_older_audio(daemon, monkeypatch, stub_playback, tmp_path):
    """A superseded line must not play: the caller has moved past it."""

    async def scenario():
        gate = asyncio.Event()

        async def held_synthesis(text, voice, speed, lang):
            await gate.wait()
            path = tmp_path / "stale.wav"
            path.write_bytes(b"")
            return str(path)

        monkeypatch.setattr(daemon, "_synthesize", held_synthesis)
        slow = asyncio.create_task(daemon.speak("The stale line."))
        await asyncio.sleep(0)
        # A second request arrives while the first is still synthesizing.
        daemon._request_id += 1
        gate.set()
        return await slow

    assert asyncio.run(scenario()) is False
    assert stub_playback.played == []


def test_failed_synthesis_reports_false_and_plays_nothing(daemon, monkeypatch, stub_playback):
    async def failed(*args):
        return None

    monkeypatch.setattr(daemon, "_synthesize", failed)
    assert asyncio.run(daemon.speak("Unrenderable.")) is False
    assert stub_playback.played == []


def test_text_longer_than_the_cap_is_truncated(daemon):
    async def scenario():
        reply = daemon._handle_request({"text": "x" * (kokoro_daemon.MAX_TEXT_CHARS + 50)})
        await asyncio.sleep(0)
        return reply

    reply = asyncio.run(scenario())
    assert reply == {"ok": True, "queued": True}
    assert len(daemon.calls[0]["text"]) == kokoro_daemon.MAX_TEXT_CHARS


def test_empty_and_unknown_requests_are_rejected(daemon):
    async def scenario():
        return (
            daemon._handle_request({"text": "   "}),
            daemon._handle_request({"command": "sing"}),
        )

    empty, unknown = asyncio.run(scenario())
    assert empty["ok"] is False and "no text" in empty["error"].lower()
    assert unknown["ok"] is False and "sing" in unknown["error"]


async def _round_trip(path, payload):
    reader, writer = await asyncio.open_unix_connection(path)
    writer.write((json.dumps(payload) + "\n").encode("utf-8"))
    await writer.drain()
    line = await reader.readline()
    writer.close()
    return json.loads(line.decode("utf-8"))


def test_protocol_answers_ping_stop_and_shutdown(daemon, sock_dir, stub_playback):
    path = str(sock_dir / "d.sock")

    async def scenario():
        server = await asyncio.start_unix_server(daemon.handle_client, path=path)
        async with server:
            ping = await _round_trip(path, {"command": "ping"})
            stop = await _round_trip(path, {"command": "stop"})
            shutdown = await _round_trip(path, {"command": "shutdown"})
        return ping, stop, shutdown

    ping, stop, shutdown = asyncio.run(scenario())
    assert ping["ok"] is True and ping["pid"] == os.getpid()
    assert stop == {"ok": True} and stub_playback.stopped == 1
    assert shutdown == {"ok": True} and daemon._shutdown.is_set()


def test_a_speak_request_returns_before_the_audio_does(daemon, monkeypatch, sock_dir, stub_playback):
    """The hook contract: acknowledge without waiting for synthesis to finish.

    Synthesis is held open for the whole exchange, so a reply can only arrive
    if the daemon answered without waiting for it.
    """
    path = str(sock_dir / "d.sock")

    async def scenario():
        gate = asyncio.Event()
        spoken = []

        async def held_synthesis(text, voice, speed, lang):
            spoken.append(text)
            await gate.wait()
            return None

        monkeypatch.setattr(daemon, "_synthesize", held_synthesis)
        server = await asyncio.start_unix_server(daemon.handle_client, path=path)
        async with server:
            reply = await _round_trip(path, {"text": "Speak this."})
            still_playing = list(stub_playback.played)
            gate.set()
            await asyncio.sleep(0)
        return reply, spoken, still_playing

    reply, spoken, still_playing = asyncio.run(scenario())
    assert reply == {"ok": True, "queued": True}
    assert spoken == ["Speak this."]
    assert still_playing == []


def test_waiting_requests_block_until_the_line_has_played(daemon, sock_dir, stub_playback):
    path = str(sock_dir / "d.sock")

    async def scenario():
        server = await asyncio.start_unix_server(daemon.handle_client, path=path)
        async with server:
            return await _round_trip(path, {"text": "Wait for me.", "wait": True})

    assert asyncio.run(scenario()) == {"ok": True}
    assert len(stub_playback.played) == 1


def test_malformed_requests_get_an_error_not_a_dropped_connection(daemon, sock_dir):
    path = str(sock_dir / "d.sock")

    async def scenario():
        server = await asyncio.start_unix_server(daemon.handle_client, path=path)
        async with server:
            reader, writer = await asyncio.open_unix_connection(path)
            writer.write(b"not json at all\n")
            await writer.drain()
            bad_json = json.loads((await reader.readline()).decode("utf-8"))
            writer.close()
            not_an_object = await _round_trip(path, ["speak"])
        return bad_json, not_an_object

    bad_json, not_an_object = asyncio.run(scenario())
    assert bad_json["ok"] is False and "JSON" in bad_json["error"]
    assert not_an_object["ok"] is False and "object" in not_an_object["error"]


def test_a_socket_file_with_nothing_behind_it_is_removed(sock_dir):
    stale = sock_dir / "d.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(stale))
    server.close()  # leaves the file, nothing listening
    assert stale.exists()
    kokoro_daemon._clear_stale_socket(str(stale))
    assert not stale.exists()


def test_a_live_socket_is_refused_rather_than_hijacked(sock_dir):
    live = sock_dir / "d.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(live))
    server.listen(1)
    try:
        with pytest.raises(SystemExit, match="already listening"):
            kokoro_daemon._clear_stale_socket(str(live))
        assert live.exists()
    finally:
        server.close()
        live.unlink(missing_ok=True)
