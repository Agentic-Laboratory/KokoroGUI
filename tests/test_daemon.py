"""Daemon protocol and dispatch tests.

The engine is stubbed throughout: none of this needs a model, and the suite's
policy is that a default run downloads nothing and touches no audio device.
Queue ordering, overflow, replace and dedupe live in tests/test_daemon_queue.py
so this file stays the protocol-shape suite.
"""
import asyncio
import json
import os
import shutil
import socket
import tempfile
import time
from pathlib import Path

import pytest

import kokoro_daemon
import kokoro_history


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
def store(tmp_path):
    """A store under tmp_path, never the real $TMPDIR/kokoro-ttsd."""
    return kokoro_history.HistoryStore(tmp_path / "store")


@pytest.fixture
def daemon(monkeypatch, stub_playback, store):
    """A daemon whose synthesis writes a placeholder file instead of audio."""
    instance = kokoro_daemon.SynthesisDaemon(store=store)
    instance.engine = object()

    calls = []

    async def fake_synthesize(text, voice, speed, lang, path):
        calls.append(
            {"text": text, "voice": voice, "speed": speed, "lang": lang, "path": path}
        )
        Path(path).write_bytes(b"audio")
        return True

    monkeypatch.setattr(instance, "_synthesize", fake_synthesize)
    instance.calls = calls
    return instance


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


async def _settle(predicate, timeout=5.0):
    """Give the render and consumer tasks turns until `predicate` holds."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.005)
    return False


def _seed_history(store, count, fmt="ogg", payload=b"audio"):
    """Record `count` entries with real placeholder audio behind each."""
    for index in range(count):
        entry_id = store.next_id()
        path = store.path_for(entry_id, fmt)
        Path(path).write_bytes(payload)
        store.record(entry_id, f"stored line {index}", "af_sky", fmt, path)


def test_socket_path_prefers_explicit_then_environment(monkeypatch):
    monkeypatch.setenv("KOKORO_TTS_SOCKET", "/tmp/from-env.sock")
    assert kokoro_daemon.socket_path("/tmp/explicit.sock") == "/tmp/explicit.sock"
    assert kokoro_daemon.socket_path() == "/tmp/from-env.sock"
    monkeypatch.delenv("KOKORO_TTS_SOCKET")
    assert kokoro_daemon.socket_path() == str(kokoro_daemon.DAEMON_SOCKET)


# ---------------------------------------------------------------------------
# speak: accept, render, play
# ---------------------------------------------------------------------------

def test_a_speak_request_is_acknowledged_with_an_id_then_played(daemon, stub_playback):
    async def scenario():
        reply = daemon._handle_request({"text": "A finished line."})
        assert await _settle(lambda: len(stub_playback.played) == 1)
        return reply

    reply = asyncio.run(scenario())
    assert reply == {"ok": True, "queued": True, "id": "0001"}
    assert len(daemon.calls) == 1
    assert daemon.calls[0]["text"] == "A finished line."
    assert daemon.calls[0]["voice"] == kokoro_daemon.DEFAULT_VOICE
    assert daemon.calls[0]["speed"] == kokoro_daemon.DEFAULT_SPEED
    assert daemon.calls[0]["lang"] == kokoro_daemon.DEFAULT_LANG
    assert stub_playback.played[0][1] is True


def test_a_speak_request_overrides_voice_speed_and_language(daemon, stub_playback):
    async def scenario():
        daemon._handle_request(
            {"text": "Une ligne.", "voice": "ff_siwis", "speed": 1.2, "lang": "f"}
        )
        assert await _settle(lambda: len(stub_playback.played) == 1)

    asyncio.run(scenario())
    assert daemon.calls[0]["voice"] == "ff_siwis"
    assert daemon.calls[0]["speed"] == 1.2
    assert daemon.calls[0]["lang"] == "f"


def test_the_rendered_file_is_kept_as_history_rather_than_unlinked(daemon, store, stub_playback):
    """The old daemon deleted its audio; keeping it is what makes replay work."""

    async def scenario():
        daemon._handle_request({"text": "Recoverable."})
        assert await _settle(lambda: len(stub_playback.played) == 1)

    asyncio.run(scenario())
    played_path = Path(stub_playback.played[0][0])
    assert played_path.exists()
    assert played_path == Path(store.path_for("0001", kokoro_daemon.DEFAULT_FORMAT))
    assert [entry["id"] for entry in store.entries()] == ["0001"]


def test_failed_synthesis_plays_nothing_and_records_nothing(daemon, monkeypatch, store, stub_playback):
    async def failed(text, voice, speed, lang, path):
        return False

    monkeypatch.setattr(daemon, "_synthesize", failed)

    async def scenario():
        reply = daemon._handle_request({"text": "Unrenderable."})
        await _settle(lambda: False, timeout=0.05)
        return reply

    reply = asyncio.run(scenario())
    assert reply == {"ok": True, "queued": True, "id": "0001"}
    assert stub_playback.played == []
    assert store.entries() == []


def test_text_longer_than_the_cap_is_truncated(daemon, stub_playback):
    async def scenario():
        reply = daemon._handle_request({"text": "x" * (kokoro_daemon.MAX_TEXT_CHARS + 50)})
        assert await _settle(lambda: len(daemon.calls) == 1)
        return reply

    reply = asyncio.run(scenario())
    assert reply == {"ok": True, "queued": True, "id": "0001"}
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


# ---------------------------------------------------------------------------
# format
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("fmt", kokoro_daemon.ACCEPTED_FORMATS)
def test_each_accepted_format_names_the_store_file_after_itself(daemon, fmt, stub_playback):
    async def scenario():
        reply = daemon._handle_request({"text": "Encoded.", "format": fmt})
        assert await _settle(lambda: len(daemon.calls) == 1)
        return reply

    reply = asyncio.run(scenario())
    assert reply["ok"] is True
    assert Path(daemon.calls[0]["path"]).suffix == f".{fmt}"


def test_a_per_request_format_does_not_change_the_daemon_default(daemon, stub_playback):
    async def scenario():
        daemon._handle_request({"text": "One wav.", "format": "wav"})
        assert await _settle(lambda: len(daemon.calls) == 1)
        daemon._handle_request({"text": "Back to the default."})
        assert await _settle(lambda: len(daemon.calls) == 2)

    asyncio.run(scenario())
    assert daemon.format == kokoro_daemon.DEFAULT_FORMAT
    assert Path(daemon.calls[1]["path"]).suffix == f".{kokoro_daemon.DEFAULT_FORMAT}"


def test_an_unknown_format_is_rejected_before_an_id_or_a_file_exists(daemon, store, tmp_path):
    async def scenario():
        return daemon._handle_request({"command": "speak", "text": "x", "format": "aiff"})

    reply = asyncio.run(scenario())
    assert reply["ok"] is False
    assert "aiff" in reply["error"]
    assert daemon.calls == []
    assert store.entries() == []
    # Nothing was reserved, so the next accepted line still gets 0001.
    assert store.next_id() == "0001"
    assert list((tmp_path / "store").glob("*.*")) == []


def test_the_serve_parser_resolves_the_format_from_the_flag_then_the_environment(monkeypatch):
    parser = kokoro_daemon.build_parser()
    assert parser.parse_args(["serve", "--format", "wav"]).format == "wav"
    assert parser.parse_args(["serve"]).format == kokoro_daemon.DEFAULT_FORMAT

    monkeypatch.setenv("KOKORO_DAEMON_FORMAT", "mp3")
    reparsed = kokoro_daemon.build_parser()
    assert reparsed.parse_args(["serve"]).format == "mp3"
    assert reparsed.parse_args(["serve", "--format", "ogg"]).format == "ogg"


def test_a_bare_invocation_still_has_a_format_to_serve_with(monkeypatch):
    """`kokoro-ttsd` with no subcommand goes through main()'s default loop."""
    served = {}

    async def fake_serve(args):
        served["format"] = args.format
        served["voice"] = args.voice

    monkeypatch.setattr(kokoro_daemon, "_serve", fake_serve)
    assert kokoro_daemon.main([]) == 0
    assert served == {"format": kokoro_daemon.DEFAULT_FORMAT, "voice": kokoro_daemon.DEFAULT_VOICE}


# ---------------------------------------------------------------------------
# ping, history, replay, hold, purge
# ---------------------------------------------------------------------------

def test_ping_reports_the_format_the_hold_state_the_queue_and_the_history(daemon, store):
    _seed_history(store, 3, payload=b"0123456789")
    store.set_held(True)

    async def scenario():
        return daemon._handle_request({"command": "ping"})

    reply = asyncio.run(scenario())
    assert reply["ok"] is True
    assert reply["pid"] == os.getpid()
    assert reply["voice"] == kokoro_daemon.DEFAULT_VOICE
    assert reply["lang"] == kokoro_daemon.DEFAULT_LANG
    assert reply["format"] == kokoro_daemon.DEFAULT_FORMAT
    assert reply["held"] is True
    assert reply["queued"] == 0
    assert reply["history_count"] == 3
    assert reply["history_bytes"] == 30


def test_pings_queued_field_counts_pending_jobs_not_the_one_playing(daemon, store, monkeypatch):
    """`queued` is the pending depth, not the boolean a speak ack carries."""

    async def scenario():
        # Nothing consumes, so every enqueued job stays pending and countable.
        monkeypatch.setattr(daemon, "_ensure_consumer", lambda: None)
        for index in range(3):
            daemon._handle_request({"text": f"line {index}"})
        assert await _settle(lambda: daemon._queue.qsize() == 3)
        return daemon._handle_request({"command": "ping"})

    assert asyncio.run(scenario())["queued"] == 3


def test_history_is_newest_first_honours_a_limit_and_defaults_to_fifteen(daemon, store):
    _seed_history(store, 20)

    async def scenario():
        return (
            daemon._handle_request({"command": "history"}),
            daemon._handle_request({"command": "history", "limit": 2}),
        )

    default, limited = asyncio.run(scenario())
    assert default["ok"] is True
    assert len(default["entries"]) == kokoro_daemon.HISTORY_LIMIT_DEFAULT == 15
    assert [entry["id"] for entry in limited["entries"]] == ["0020", "0019"]
    assert set(limited["entries"][0]) == {"id", "text", "voice", "format", "bytes", "created"}


def test_history_on_an_empty_store_is_an_empty_list_not_an_error(daemon):
    async def scenario():
        return daemon._handle_request({"command": "history"})

    assert asyncio.run(scenario()) == {"ok": True, "entries": []}


@pytest.mark.parametrize("limit", [0, -1, "5", 1.5, None, True])
def test_history_rejects_a_limit_that_is_not_a_positive_integer(daemon, limit):
    async def scenario():
        return daemon._handle_request({"command": "history", "limit": limit})

    reply = asyncio.run(scenario())
    assert reply["ok"] is False
    assert "positive integer" in reply["error"]


def test_replay_plays_a_stored_entry_without_synthesizing_it(daemon, store, stub_playback):
    _seed_history(store, 2)

    async def scenario():
        reply = daemon._handle_request({"command": "replay", "id": "0001"})
        assert await _settle(lambda: len(stub_playback.played) == 1)
        return reply

    assert asyncio.run(scenario()) == {"ok": True}
    assert daemon.calls == []
    assert stub_playback.played[0][0] == store.path_for("0001", "ogg")


def test_replay_ignores_the_hold_marker(daemon, store, stub_playback):
    """Hold exists so a user can come back to a line, not so replay refuses."""
    _seed_history(store, 1)
    store.set_held(True)

    async def scenario():
        reply = daemon._handle_request({"command": "replay", "id": "0001"})
        assert await _settle(lambda: len(stub_playback.played) == 1)
        return reply

    assert asyncio.run(scenario()) == {"ok": True}


def test_replay_reports_a_missing_id_an_unknown_id_and_missing_audio_apart(daemon, store):
    _seed_history(store, 1)
    Path(store.path_for("0001", "ogg")).unlink()

    async def scenario():
        return (
            daemon._handle_request({"command": "replay"}),
            daemon._handle_request({"command": "replay", "id": "0099"}),
            daemon._handle_request({"command": "replay", "id": "0001"}),
        )

    no_id, unknown, gone = asyncio.run(scenario())
    assert no_id == {"ok": False, "error": "replay requires an id."}
    assert unknown == {"ok": False, "error": "No history entry 0099."}
    assert gone == {"ok": False, "error": "Audio for 0001 is gone."}


def test_hold_and_release_are_idempotent_and_report_the_state(daemon, store):
    async def scenario():
        return [
            daemon._handle_request({"command": "hold"}),
            daemon._handle_request({"command": "hold"}),
            daemon._handle_request({"command": "release"}),
            daemon._handle_request({"command": "release"}),
        ]

    held_once, held_twice, released_once, released_twice = asyncio.run(scenario())
    assert held_once == held_twice == {"ok": True, "held": True}
    assert released_once == released_twice == {"ok": True, "held": False}
    assert store.held() is False


def test_purge_stops_playback_drains_the_queue_and_reports_what_it_removed(
    daemon, store, stub_playback, monkeypatch, tmp_path
):
    _seed_history(store, 4, payload=b"0123456789")

    async def scenario():
        monkeypatch.setattr(daemon, "_ensure_consumer", lambda: None)
        daemon._handle_request({"text": "pending"})
        assert await _settle(lambda: daemon._queue.qsize() == 1)
        return daemon._handle_request({"command": "purge"})

    reply = asyncio.run(scenario())
    # Five files: the four seeded plus the one the pending job rendered.
    assert reply == {"ok": True, "removed": 5, "bytes": 45}
    assert stub_playback.stopped == 1
    assert daemon._queue.qsize() == 0
    assert list((tmp_path / "store").glob("*.ogg")) == []
    assert (tmp_path / "store" / kokoro_history.INDEX_NAME).read_text() == ""


def test_purge_does_not_reset_the_ids_a_client_may_still_hold(daemon, store):
    _seed_history(store, 2)

    async def scenario():
        return daemon._handle_request({"command": "purge"})

    asyncio.run(scenario())
    assert store.next_id() == "0003"


# ---------------------------------------------------------------------------
# the socket
# ---------------------------------------------------------------------------

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

        async def held_synthesis(text, voice, speed, lang, destination):
            spoken.append(text)
            await gate.wait()
            return False

        monkeypatch.setattr(daemon, "_synthesize", held_synthesis)
        server = await asyncio.start_unix_server(daemon.handle_client, path=path)
        async with server:
            reply = await _round_trip(path, {"text": "Speak this."})
            still_playing = list(stub_playback.played)
            gate.set()
            await asyncio.sleep(0)
        return reply, spoken, still_playing

    reply, spoken, still_playing = asyncio.run(scenario())
    assert reply == {"ok": True, "queued": True, "id": "0001"}
    assert spoken == ["Speak this."]
    assert still_playing == []


def test_waiting_requests_block_until_the_line_has_played(daemon, sock_dir, stub_playback):
    path = str(sock_dir / "d.sock")

    async def scenario():
        server = await asyncio.start_unix_server(daemon.handle_client, path=path)
        async with server:
            return await _round_trip(path, {"text": "Wait for me.", "wait": True})

    assert asyncio.run(scenario()) == {"ok": True, "queued": True, "id": "0001"}
    assert len(stub_playback.played) == 1


def test_a_held_request_with_wait_answers_at_once(daemon, store, sock_dir, stub_playback):
    """There is nothing to wait for: the line was kept, not queued."""
    store.set_held(True)
    path = str(sock_dir / "d.sock")

    async def scenario():
        server = await asyncio.start_unix_server(daemon.handle_client, path=path)
        async with server:
            reply = await _round_trip(path, {"text": "Kept for later.", "wait": True})
            assert await _settle(lambda: len(store.entries()) == 1)
        return reply

    assert asyncio.run(scenario()) == {"ok": True, "held": True, "id": "0001"}
    assert stub_playback.played == []


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


# ---------------------------------------------------------------------------
# the CLI surface
# ---------------------------------------------------------------------------

def test_say_sends_format_key_and_replace_only_when_they_are_set(monkeypatch):
    sent = []

    def fake_client_command(payload, args):
        sent.append(payload)
        return 0

    monkeypatch.setattr(kokoro_daemon, "_client_command", fake_client_command)

    assert kokoro_daemon.main(["say", "Plain."]) == 0
    assert sent[-1] == {"command": "speak", "text": "Plain.", "wait": False}

    kokoro_daemon.main(
        ["say", "Loud.", "--format", "wav", "--key", "abc123", "--replace", "--wait"]
    )
    assert sent[-1] == {
        "command": "speak",
        "text": "Loud.",
        "wait": True,
        "format": "wav",
        "key": "abc123",
        "replace": True,
    }


def test_the_new_control_commands_are_bare_round_trips(monkeypatch):
    sent = []
    monkeypatch.setattr(
        kokoro_daemon, "_client_command", lambda payload, args: sent.append(payload) or 0
    )
    for name in ("ping", "stop", "shutdown", "hold", "release", "purge"):
        assert kokoro_daemon.main([name]) == 0
        assert sent[-1] == {"command": name}


def test_history_and_replay_carry_their_arguments(monkeypatch):
    sent = []
    monkeypatch.setattr(
        kokoro_daemon, "_client_command", lambda payload, args: sent.append(payload) or 0
    )
    kokoro_daemon.main(["history"])
    assert sent[-1] == {"command": "history", "limit": kokoro_daemon.HISTORY_LIMIT_DEFAULT}
    kokoro_daemon.main(["history", "--limit", "3"])
    assert sent[-1] == {"command": "history", "limit": 3}
    kokoro_daemon.main(["replay", "0042"])
    assert sent[-1] == {"command": "replay", "id": "0042"}


def test_the_new_subcommands_accept_socket_and_timeout_after_the_name():
    """kokoro_menubar builds its commands that way round; keep it working."""
    parser = kokoro_daemon.build_parser()
    for argv in (
        ["hold", "--socket", "/tmp/x.sock", "--timeout", "2"],
        ["release", "--socket", "/tmp/x.sock"],
        ["purge", "--timeout", "1.5"],
        ["history", "--limit", "5", "--socket", "/tmp/x.sock"],
        ["replay", "0001", "--socket", "/tmp/x.sock"],
    ):
        args = parser.parse_args(argv)
        assert args.command == argv[0]
