"""Queue, overflow, replace, dedupe and hold behaviour of the daemon.

Kept apart from tests/test_daemon.py, which stays the protocol-shape suite.
Everything here is mocked: the engine never runs, and playback is a stub whose
`play` blocks on a threading.Event exactly as the real blocking call does, so
the queue can be filled deterministically instead of by luck.

Every scenario that leaves the gate shut releases it before returning.
`asyncio.run` joins the default thread pool on the way out, so a parked
`play` would otherwise stall the test for the length of the gate's timeout.
"""
import asyncio
import json
import shutil
import tempfile
import threading
import time
from pathlib import Path

import pytest

import kokoro_daemon
import kokoro_history


class GatedPlayback:
    """A playback stub whose `play` blocks until it is released.

    The consumer calls `play(path, True)` through `asyncio.to_thread`, so the
    gate has to be a threading primitive, not an asyncio one. `stop()` releases
    only the call in flight and then re-arms, mirroring the real module, where
    `stop()` bumps a request id that makes the current blocking play return
    early without affecting the next one.
    """

    def __init__(self):
        self.played = []
        self.stopped = 0
        self._release = threading.Event()
        self._interrupted = False

    def play(self, path, blocking=False):
        self.played.append((path, blocking))
        # Bounded, so a failing assertion cannot cost the whole timeout.
        self._release.wait(timeout=5)
        if self._interrupted:
            self._interrupted = False
            self._release.clear()

    def stop(self):
        self.stopped += 1
        self._interrupted = True
        self._release.set()

    def release(self):
        """Let this and every later line through."""
        self._interrupted = False
        self._release.set()

    @property
    def paths(self):
        return [entry[0] for entry in self.played]


class SerialProbe:
    """A playback stub that fails loudly the instant two `play` calls overlap.

    `GatedPlayback` proves arrival order but not exclusion: with the gate
    released, `play` returns instantly, so two calls could interleave on
    separate threads without any assertion there noticing. This stub holds a
    non-reentrant lock across the body of `play` and records a strictly
    alternating enter/exit log, so a regression that let `_consume` start a
    second line before the first one returned would show up as a failed
    acquire rather than passing by accident.
    """

    def __init__(self):
        self.events = []
        self.overlaps = []
        self._lock = threading.Lock()

    def play(self, path, blocking=False):
        if not self._lock.acquire(blocking=False):
            self.overlaps.append(path)
            raise RuntimeError(f"playback overlap on {path}")
        try:
            self.events.append(("enter", path))
            # Real sleep, not the GatedPlayback event: this runs in the
            # to_thread worker, and only wall-clock time gives a second call
            # a window to land while this one is still inside the lock.
            time.sleep(0.02)
            self.events.append(("exit", path))
        finally:
            self._lock.release()

    def stop(self):
        pass  # not exercised here; _render, `stop` and `purge` all call it


@pytest.fixture
def gated(monkeypatch):
    stub = GatedPlayback()
    monkeypatch.setattr(kokoro_daemon, "playback", stub)
    try:
        yield stub
    finally:
        stub.release()


@pytest.fixture
def store(tmp_path):
    return kokoro_history.HistoryStore(tmp_path / "store")


@pytest.fixture
def daemon(monkeypatch, gated, store):
    instance = kokoro_daemon.SynthesisDaemon(store=store)
    instance.engine = object()

    calls = []

    async def fake_synthesize(text, voice, speed, lang, path):
        calls.append({"text": text, "path": path})
        Path(path).write_bytes(b"audio")
        return True

    monkeypatch.setattr(instance, "_synthesize", fake_synthesize)
    instance.calls = calls
    return instance


@pytest.fixture
def tight_queue(monkeypatch, daemon):
    """Shrink the cap back to 5 for scenarios that overflow it by hand.

    `QUEUE_MAXSIZE` moved to 100 as a runaway safety valve, but a handful of
    overflow tests want a small, readable number of lines to fill it with, not
    102. Rebuilding `_queue` is required alongside the monkeypatch: the daemon
    fixture already read the old value into `asyncio.Queue(maxsize=...)` in
    `__init__`, and nothing is consuming from it yet, so replacing it here is
    safe.
    """
    monkeypatch.setattr(kokoro_daemon, "QUEUE_MAXSIZE", 5)
    daemon._queue = asyncio.Queue(maxsize=5)
    return daemon


@pytest.fixture
def sock_dir():
    directory = tempfile.mkdtemp(prefix="kq-", dir="/tmp")
    try:
        yield Path(directory)
    finally:
        shutil.rmtree(directory, ignore_errors=True)


async def _settle(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.005)
    return False


async def _round_trip(path, payload):
    reader, writer = await asyncio.open_unix_connection(path)
    writer.write((json.dumps(payload) + "\n").encode("utf-8"))
    await writer.drain()
    line = await reader.readline()
    writer.close()
    return json.loads(line.decode("utf-8"))


def _index_ids(store):
    """Oldest first, as the index file itself is ordered."""
    return [entry["id"] for entry in reversed(store.entries(limit=None))]


# ---------------------------------------------------------------------------
# ordering
# ---------------------------------------------------------------------------

def test_queued_lines_play_in_arrival_order(daemon, gated, store):
    """The supersede rule is gone: three lines mean three lines, oldest first."""
    gated.release()

    async def scenario():
        replies = [daemon._handle_request({"text": f"line {n}"}) for n in (1, 2, 3)]
        assert await _settle(lambda: len(gated.played) == 3)
        return replies

    replies = asyncio.run(scenario())
    assert [reply["id"] for reply in replies] == ["0001", "0002", "0003"]
    assert gated.paths == [
        store.path_for("0001", "ogg"),
        store.path_for("0002", "ogg"),
        store.path_for("0003", "ogg"),
    ]
    assert all(blocking is True for _, blocking in gated.played)


def test_playback_never_overlaps_across_queued_lines(daemon, monkeypatch, store):
    """Pins the serial guarantee live testing already showed: `_consume` is one
    loop awaiting one blocking `to_thread` call, so a line cannot start until
    the one ahead of it has finished playing. That was previously only
    verified by hand; the user asked for the guarantee explicitly, so it
    needs a regression test rather than a comment.
    """
    probe = SerialProbe()
    # Replaces the `gated` stub the `daemon` fixture installed: both write to
    # the same `kokoro_daemon.playback` global, and `_playback_module()`
    # re-reads it on every call.
    monkeypatch.setattr(kokoro_daemon, "playback", probe)

    async def scenario():
        ids = [daemon._handle_request({"text": f"line {n}"})["id"] for n in range(1, 6)]
        assert await _settle(lambda: len(probe.events) == 10)
        return ids

    ids = asyncio.run(scenario())
    assert ids == ["0001", "0002", "0003", "0004", "0005"]
    assert probe.overlaps == []
    expected = []
    for entry_id in ids:
        path = store.path_for(entry_id, "ogg")
        expected.append(("enter", path))
        expected.append(("exit", path))
    assert probe.events == expected


# ---------------------------------------------------------------------------
# overflow
# ---------------------------------------------------------------------------

def test_overflow_drops_the_oldest_pending_line_and_keeps_it_replayable(daemon, gated, store, tight_queue):
    async def scenario():
        try:
            first = daemon._handle_request({"text": "line 1"})
            # The consumer is now parked inside play() for line 1, so
            # everything below accumulates as pending rather than draining
            # behind our back.
            assert await _settle(lambda: len(gated.played) == 1)
            ids = [first["id"]]
            for index in range(2, 8):
                ids.append(daemon._handle_request({"text": f"line {index}"})["id"])
                expected = min(index - 1, kokoro_daemon.QUEUE_MAXSIZE)
                assert await _settle(lambda want=expected: daemon._queue.qsize() == want)
                assert daemon._queue.qsize() <= kokoro_daemon.QUEUE_MAXSIZE
        finally:
            gated.release()
        assert await _settle(lambda: len(gated.played) == 6)
        return ids

    ids = asyncio.run(scenario())
    assert ids == ["0001", "0002", "0003", "0004", "0005", "0006", "0007"]
    # 0002 was the oldest pending when the seventh arrived, so it is the one
    # that went - not the newest, and not the line already playing.
    assert gated.paths == [store.path_for(entry_id, "ogg") for entry_id in
                           ["0001", "0003", "0004", "0005", "0006", "0007"]]
    # It was rendered and recorded before the enqueue, so it is still there.
    assert "0002" in _index_ids(store)
    assert Path(store.path_for("0002", "ogg")).exists()


def test_a_wait_request_whose_job_is_dropped_still_gets_its_reply(daemon, gated, store, sock_dir, tight_queue):
    """Without dropped.done.set() this client would hang until its timeout."""
    path = str(sock_dir / "d.sock")

    async def scenario():
        server = await asyncio.start_unix_server(daemon.handle_client, path=path)
        async with server:
            try:
                daemon._handle_request({"text": "line 1"})
                assert await _settle(lambda: len(gated.played) == 1)
                waiter = asyncio.create_task(
                    _round_trip(path, {"text": "line 2", "wait": True})
                )
                assert await _settle(lambda: daemon._queue.qsize() == 1)
                for index in range(3, 8):
                    daemon._handle_request({"text": f"line {index}"})
                assert await _settle(
                    lambda: daemon._queue.qsize() == kokoro_daemon.QUEUE_MAXSIZE
                )
                # The reply has to arrive here, while the gate is still shut
                # and line 2 has certainly not played.
                reply = await asyncio.wait_for(waiter, timeout=5)
                unheard = store.path_for("0002", "ogg") not in gated.paths
            finally:
                gated.release()
        return reply, unheard

    reply, unheard = asyncio.run(scenario())
    assert reply == {"ok": True, "queued": True, "id": "0002"}
    assert unheard is True


def test_overflow_still_drops_the_oldest_at_the_real_default_cap(daemon, gated, store):
    """The other overflow tests shrink the cap for readability; this one does
    not, so the real 100-deep default stays covered by something.
    """

    async def scenario():
        try:
            first = daemon._handle_request({"text": "line 1"})
            assert await _settle(lambda: len(gated.played) == 1)
            # cap + 1 more lines behind the one already playing: cap of them
            # exactly fill the queue, and the last is what forces the drop,
            # same shape as the 7-line version of this scenario at cap 5.
            for index in range(2, kokoro_daemon.QUEUE_MAXSIZE + 3):
                daemon._handle_request({"text": f"line {index}"})
            assert await _settle(lambda: daemon._queue.qsize() == kokoro_daemon.QUEUE_MAXSIZE)
        finally:
            gated.release()
        assert await _settle(lambda: len(gated.played) == kokoro_daemon.QUEUE_MAXSIZE + 1)

    asyncio.run(scenario())
    # id 0002 was the oldest pending when the cap was exceeded, so it is the
    # one that went, exactly as at the smaller cap.
    assert store.path_for("0002", "ogg") not in gated.paths
    assert "0002" in _index_ids(store)
    assert Path(store.path_for("0002", "ogg")).exists()


# ---------------------------------------------------------------------------
# replace and stop
# ---------------------------------------------------------------------------

def test_replace_interrupts_the_current_line_and_clears_the_backlog(daemon, gated, store):
    async def scenario():
        try:
            daemon._handle_request({"text": "line 1"})
            assert await _settle(lambda: len(gated.played) == 1)
            daemon._handle_request({"text": "line 2"})
            daemon._handle_request({"text": "line 3"})
            assert await _settle(lambda: daemon._queue.qsize() == 2)
            reply = daemon._handle_request({"text": "urgent", "replace": True})
            assert await _settle(lambda: len(gated.played) == 2)
            return reply
        finally:
            gated.release()

    reply = asyncio.run(scenario())
    assert reply == {"ok": True, "queued": True, "id": "0004"}
    assert gated.stopped == 1
    assert gated.paths == [store.path_for("0001", "ogg"), store.path_for("0004", "ogg")]
    assert daemon._queue.qsize() == 0
    # The two dropped lines were recorded before the drain, so they survive.
    assert _index_ids(store) == ["0001", "0002", "0003", "0004"]


def test_stop_drains_the_queue_so_the_next_line_does_not_start(daemon, gated, store):
    async def scenario():
        try:
            daemon._handle_request({"text": "line 1"})
            assert await _settle(lambda: len(gated.played) == 1)
            daemon._handle_request({"text": "line 2"})
            daemon._handle_request({"text": "line 3"})
            assert await _settle(lambda: daemon._queue.qsize() == 2)
            reply = daemon._handle_request({"command": "stop"})
            # Long enough for the consumer to come back from the interrupted
            # play and find nothing waiting.
            await asyncio.sleep(0.1)
            return reply
        finally:
            gated.release()

    assert asyncio.run(scenario()) == {"ok": True}
    assert gated.stopped == 1
    assert gated.paths == [store.path_for("0001", "ogg")]
    assert daemon._queue.qsize() == 0
    assert _index_ids(store) == ["0001", "0002", "0003"]


# ---------------------------------------------------------------------------
# dedupe
# ---------------------------------------------------------------------------

def test_a_repeated_key_is_ignored_without_reserving_an_id_or_a_file(daemon, gated, store):
    gated.release()

    async def scenario():
        first = daemon._handle_request({"text": "Exec summary: done.", "key": "msg-1"})
        assert await _settle(lambda: len(gated.played) == 1)
        second = daemon._handle_request({"text": "Exec summary: done.", "key": "msg-1"})
        await asyncio.sleep(0.05)
        return first, second

    first, second = asyncio.run(scenario())
    assert first == {"ok": True, "queued": True, "id": "0001"}
    assert second == {"ok": True, "deduped": True}
    assert len(daemon.calls) == 1
    assert _index_ids(store) == ["0001"]
    # No id was burned by the duplicate.
    assert store.next_id() == "0002"


def test_a_request_with_no_key_is_never_deduplicated(daemon, gated):
    """`kokoro-ttsd say` and build notifiers repeat the same text on purpose."""
    gated.release()

    async def scenario():
        daemon._handle_request({"text": "Build green."})
        daemon._handle_request({"text": "Build green."})
        assert await _settle(lambda: len(gated.played) == 2)

    asyncio.run(scenario())
    assert len(daemon.calls) == 2


def test_the_key_set_is_bounded_and_evicts_in_arrival_order(daemon, gated, monkeypatch):
    monkeypatch.setattr(kokoro_daemon, "DEDUPE_KEYS", 3)
    gated.release()

    async def scenario():
        accepted = []
        for name in ("k1", "k2", "k3", "k4"):
            accepted.append(daemon._handle_request({"text": name, "key": name}))
        assert await _settle(lambda: len(gated.played) == 4)
        # k1 aged out when k4 arrived, so the same key is accepted again.
        again = daemon._handle_request({"text": "k1", "key": "k1"})
        still_known = daemon._handle_request({"text": "k3", "key": "k3"})
        assert await _settle(lambda: len(gated.played) == 5)
        return accepted, again, still_known

    accepted, again, still_known = asyncio.run(scenario())
    assert all(reply.get("queued") for reply in accepted)
    assert again == {"ok": True, "queued": True, "id": "0005"}
    assert still_known == {"ok": True, "deduped": True}


def test_a_repeat_does_not_refresh_a_keys_position_in_the_set(daemon, gated, monkeypatch):
    """Recency is arrival order, so a repeat cannot keep a key alive forever."""
    monkeypatch.setattr(kokoro_daemon, "DEDUPE_KEYS", 2)
    gated.release()

    async def scenario():
        daemon._handle_request({"text": "a", "key": "k1"})
        daemon._handle_request({"text": "b", "key": "k2"})
        daemon._handle_request({"text": "a again", "key": "k1"})  # a hit, not a touch
        daemon._handle_request({"text": "c", "key": "k3"})  # evicts k1
        assert await _settle(lambda: len(gated.played) == 3)
        return daemon._handle_request({"text": "a once more", "key": "k1"})

    assert asyncio.run(scenario())["queued"] is True


def test_replace_bypasses_the_dedupe_check_but_still_records_the_key(daemon, gated):
    gated.release()

    async def scenario():
        first = daemon._handle_request({"text": "sample", "key": "m1", "replace": True})
        assert await _settle(lambda: len(gated.played) == 1)
        repeat = daemon._handle_request({"text": "sample", "key": "m1", "replace": True})
        assert await _settle(lambda: len(gated.played) == 2)
        plain = daemon._handle_request({"text": "sample", "key": "m1"})
        await asyncio.sleep(0.05)
        return first, repeat, plain

    first, repeat, plain = asyncio.run(scenario())
    assert first["queued"] is True
    assert repeat["queued"] is True
    # Recorded by the very first replace, so an ordinary send is suppressed.
    assert plain == {"ok": True, "deduped": True}


def test_purge_is_a_history_clean_up_and_does_not_reset_the_key_set(daemon, gated):
    gated.release()

    async def scenario():
        daemon._handle_request({"text": "once", "key": "m9"})
        assert await _settle(lambda: len(gated.played) == 1)
        daemon._handle_request({"command": "purge"})
        return daemon._handle_request({"text": "once", "key": "m9"})

    assert asyncio.run(scenario()) == {"ok": True, "deduped": True}


# ---------------------------------------------------------------------------
# hold
# ---------------------------------------------------------------------------

def test_a_held_line_is_synthesized_and_recorded_but_never_played(daemon, gated, store):
    gated.release()

    async def scenario():
        daemon._handle_request({"command": "hold"})
        held = daemon._handle_request({"text": "while you were out"})
        assert await _settle(lambda: len(store.entries()) == 1)
        await asyncio.sleep(0.05)
        return held

    held = asyncio.run(scenario())
    assert held == {"ok": True, "held": True, "id": "0001"}
    assert len(daemon.calls) == 1
    assert gated.played == []
    assert Path(store.path_for("0001", "ogg")).exists()


def test_release_does_not_replay_the_backlog_it_only_lets_new_lines_through(daemon, gated, store):
    """Hold exists so eleven summaries do not burst-play on return."""
    gated.release()

    async def scenario():
        daemon._handle_request({"command": "hold"})
        daemon._handle_request({"text": "held one"})
        daemon._handle_request({"text": "held two"})
        assert await _settle(lambda: len(store.entries()) == 2)
        daemon._handle_request({"command": "release"})
        await asyncio.sleep(0.05)
        assert gated.played == []
        fresh = daemon._handle_request({"text": "after release"})
        assert await _settle(lambda: len(gated.played) == 1)
        return fresh

    fresh = asyncio.run(scenario())
    assert fresh == {"ok": True, "queued": True, "id": "0003"}
    assert gated.paths == [store.path_for("0003", "ogg")]
    # The two held lines are not lost, just unplayed.
    assert _index_ids(store) == ["0001", "0002", "0003"]


def test_hold_is_read_once_at_accept_time(daemon, gated, store, monkeypatch):
    """A line already accepted plays even if hold arrives mid-synthesis.

    The client was told `queued`; re-reading the marker after synthesis would
    make that reply a lie.
    """
    gated.release()

    async def scenario():
        gate = asyncio.Event()

        async def held_synthesis(text, voice, speed, lang, path):
            await gate.wait()
            Path(path).write_bytes(b"audio")
            return True

        monkeypatch.setattr(daemon, "_synthesize", held_synthesis)
        accepted = daemon._handle_request({"text": "already accepted"})
        await asyncio.sleep(0)
        daemon._handle_request({"command": "hold"})
        gate.set()
        assert await _settle(lambda: len(gated.played) == 1)
        return accepted

    accepted = asyncio.run(scenario())
    assert accepted == {"ok": True, "queued": True, "id": "0001"}
    assert gated.paths == [store.path_for("0001", "ogg")]
    assert store.held() is True


# ---------------------------------------------------------------------------
# history is written before the play decision
# ---------------------------------------------------------------------------

def test_held_superseded_and_dropped_lines_are_all_recorded(daemon, gated, store, monkeypatch, tight_queue):
    """Requirement F: nothing that was synthesized becomes unrecoverable."""
    records = []
    real_record = store.record

    def counting_record(*args, **kwargs):
        entry = real_record(*args, **kwargs)
        records.append(entry["id"])
        return entry

    monkeypatch.setattr(store, "record", counting_record)

    async def scenario():
        try:
            # 1. Held: accepted, rendered, recorded, never queued.
            daemon._handle_request({"command": "hold"})
            daemon._handle_request({"text": "held"})
            assert await _settle(lambda: records == ["0001"])
            daemon._handle_request({"command": "release"})

            # 2. Superseded: a pending line dropped by a replace.
            daemon._handle_request({"text": "playing"})
            assert await _settle(lambda: len(gated.played) == 1)
            daemon._handle_request({"text": "superseded"})
            assert await _settle(lambda: daemon._queue.qsize() == 1)
            daemon._handle_request({"text": "urgent", "replace": True})
            assert await _settle(lambda: len(gated.played) == 2)

            # 3. Overflow: fill the queue behind the urgent line, push one out.
            for index in range(6):
                daemon._handle_request({"text": f"flood {index}"})
                assert await _settle(lambda want=5 + index: len(records) == want)
            assert daemon._queue.qsize() == kokoro_daemon.QUEUE_MAXSIZE
        finally:
            gated.release()

    asyncio.run(scenario())
    # Every synthesized line reached history, including the ones never heard.
    assert records == [f"{number:04d}" for number in range(1, 11)]
    assert _index_ids(store) == records
    for entry_id in records:
        assert Path(store.path_for(entry_id, "ogg")).exists()
    # The held line, the superseded line and the dropped flood line never
    # reached the speaker, yet all three are on disk and replayable.
    assert store.path_for("0001", "ogg") not in gated.paths
    assert store.path_for("0003", "ogg") not in gated.paths
    assert store.path_for("0005", "ogg") not in gated.paths
