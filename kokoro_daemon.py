"""Long-lived synthesis service for callers that cannot pay a cold start.

Importing torch and building a KPipeline costs about ten seconds, and
`kokoro-tts` pays that on every invocation. A caller that wants a spoken line
the moment something finishes - an editor hook, a build notifier - cannot use
the CLI for that reason alone.

The daemon pays the cost once at startup and then answers newline-delimited
JSON requests over a Unix domain socket, so a request speaks in about a second.
It is a third front end over `KokoroEngine`, alongside `gui.py` and
`kokoro_cli.py`, and owns no synthesis logic of its own.

Three coroutines share the work, and the split is the point of the design:

    _handle_request   validate, deduplicate, reserve an id, read the hold
                      marker, reply. Synchronous, no awaits, so the whole
                      sequence is atomic against every other client.
    _render           synthesize under the lock, write history, then enqueue.
    _consume          one task, playing one line at a time, in arrival order.

Synthesis is serialized behind one lock because a single KPipeline is not safe
to drive concurrently. Playback deliberately runs outside that lock: a
two-second line must not block the synthesis of the next one.

Lines queue rather than interrupt. The daemon used to discard its own audio
whenever a newer request arrived, which meant that a caller sending several
lines per turn only ever heard the last of them. Every accepted line is now
synthesized, written to `kokoro_history`, and played in turn, so `history` and
`replay` can reach a line that was held back, superseded or dropped.
"""

import argparse
import asyncio
import collections
import dataclasses
import json
import os
import socket
import sys
import tempfile
from pathlib import Path

from kokoro_history import HistoryStore
from paths import DAEMON_SOCKET

# Resolved lazily for the same reason kokoro_cli does it: importing the engine
# pulls in torch, which would turn `kokoro-ttsd ping` from instant into ~3s.
KokoroEngine = None
playback = None

DEFAULT_VOICE = "af_heart"
DEFAULT_LANG = "a"
DEFAULT_SPEED = 1.0

# Ogg by default: a spoken status line is around 4 KB of Vorbis against 50 KB
# of WAV, and 200 of them are kept. `pedalboard` and `soundfile` both pick the
# encoder from the file extension, so nothing in the engine needs to know.
DEFAULT_FORMAT = "ogg"
ACCEPTED_FORMATS = ("wav", "ogg", "mp3")

# A spoken status line is a sentence or two. Anything longer is a document, and
# the caller should use `kokoro-tts synthesize` instead of a notification path.
MAX_TEXT_CHARS = 2000

# A runaway safety valve, not routine backpressure: one daemon serves every
# Claude Code session on the machine, and a burst of finished turns across
# several of them must queue and wait rather than be dropped, so this is not
# expected to trigger in normal use. Kept well below HISTORY_MAX_ENTRIES so a
# full queue's pending audio cannot be pruned out from under it before it plays.
QUEUE_MAXSIZE = 100        # pending jobs, not counting the one being played
DEDUPE_KEYS = 200          # message_id values remembered
HISTORY_MAX_ENTRIES = 200
HISTORY_LIMIT_DEFAULT = 15


def _engine_class():
    global KokoroEngine
    if KokoroEngine is None:
        from kokoro_engine import KokoroEngine as engine_class
        KokoroEngine = engine_class
    return KokoroEngine


def _playback_module():
    global playback
    if playback is None:
        import playback as playback_module
        playback = playback_module
    return playback


def socket_path(explicit=None):
    """Resolve the socket path from the flag, the environment, then paths.py."""
    return str(explicit or os.environ.get("KOKORO_TTS_SOCKET") or DAEMON_SOCKET)


def _status(message, is_error=False):
    print(f"{'error' if is_error else 'status'}: {message}", file=sys.stderr, flush=True)


@dataclasses.dataclass
class _Job:
    """One accepted line, from the id reservation to the end of playback.

    `held` and `replace` are decided at accept time and carried here rather
    than re-read later: the client has already been told what will happen to
    its line, and re-reading the marker after synthesis would make that reply
    a lie.
    """

    entry_id: str
    text: str
    voice: str
    speed: float
    lang: str
    fmt: str
    path: str
    held: bool = False
    replace: bool = False
    done: asyncio.Event = dataclasses.field(default_factory=asyncio.Event)


class SynthesisDaemon:
    """Holds one warm engine and speaks the text that arrives on the socket."""

    def __init__(self, voice=DEFAULT_VOICE, lang=DEFAULT_LANG, speed=DEFAULT_SPEED,
                 fmt=DEFAULT_FORMAT, store=None):
        self.voice = voice
        self.lang = lang
        self.speed = speed
        self.format = fmt
        self.engine = None
        self.store = store if store is not None else HistoryStore(max_entries=HISTORY_MAX_ENTRIES)
        self._synth_lock = asyncio.Lock()
        self._queue = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
        self._consumer_task = None
        # An insertion-ordered set. A hit deliberately does not move the key to
        # the end: recency is arrival order, so a key ages out 200 accepted
        # lines after it was first seen however many repeats it drew.
        self._seen_keys = collections.OrderedDict()
        self._shutdown = asyncio.Event()

    async def start(self):
        """Load the engine and the pipeline. Everything after this is warm."""
        # Before the first next_id(), so audio orphaned by a crash cannot be
        # mistaken for history and cannot influence the id counter.
        self.store.sweep()
        self.engine = _engine_class()()
        self.engine.on_status = lambda message, is_error: _status(message, is_error)
        if not await self.engine.init_pipeline_async(self.lang):
            raise RuntimeError(f"Pipeline initialization failed for language '{self.lang}'.")
        self._ensure_consumer()
        # Force the first inference here rather than making the first real
        # request pay for lazy weight loading inside the pipeline. Written to
        # the system temporary directory and unlinked, never into the store:
        # startup should be silent and should not pollute history.
        handle, path = tempfile.mkstemp(prefix="kokoro-daemon-", suffix=".wav")
        os.close(handle)
        try:
            await self._synthesize("Ready.", self.voice, self.speed, self.lang, path)
        finally:
            Path(path).unlink(missing_ok=True)

    def close(self):
        if self._consumer_task is not None:
            self._consumer_task.cancel()
            self._consumer_task = None
        if self.engine is not None:
            self.engine.worker.stop()
            self.engine = None

    # -- synthesis ---------------------------------------------------------

    async def _synthesize(self, text, voice, speed, lang, path):
        """Render one line to `path`. The extension selects the encoder.

        Returns True on success. Naming the destination is the caller's job:
        the history store reserves an id first and wants the audio written
        straight into itself, with no temporary file and no cross-filesystem
        move.
        """
        config = {"lexicon": {}, "normalize": True, "trim_silence": True}
        async with self._synth_lock:
            try:
                return bool(
                    await self.engine.generate_preview(
                        text, voice, speed, path, config, lang_code=lang
                    )
                )
            except Exception as error:  # noqa: BLE001 - one bad line must not kill the daemon
                _status(f"Synthesis failed: {error}", True)
                return False

    async def _render(self, job):
        """Synthesize one accepted line, record it, then decide whether it plays.

        History is written before the play-or-hold decision, which is what
        makes a held, superseded or overflow-dropped line recoverable.
        """
        if not await self._synthesize(job.text, job.voice, job.speed, job.lang, job.path):
            Path(job.path).unlink(missing_ok=True)
            job.done.set()
            return
        self.store.record(job.entry_id, job.text, job.voice, job.fmt, job.path)
        if job.held:
            job.done.set()
            return
        if job.replace:
            # The caller asked to be heard now: cut the current line short and
            # discard the backlog, which stays in history either way.
            _playback_module().stop()
            self._drain()
        self._enqueue(job)

    async def _render_quietly(self, job):
        try:
            await self._render(job)
        except Exception as error:  # noqa: BLE001 - a background task must not take the loop down
            _status(f"Rendering entry {job.entry_id} failed: {error}", True)
            job.done.set()

    # -- the queue ---------------------------------------------------------

    def _ensure_consumer(self):
        """Start the consumer lazily.

        Not in `start()` alone, because a test builds a SynthesisDaemon and
        assigns its engine without ever calling `start()`.
        """
        if self._consumer_task is None or self._consumer_task.done():
            self._consumer_task = asyncio.get_running_loop().create_task(self._consume())

    async def _consume(self):
        while True:
            job = await self._queue.get()
            try:
                # Blocking, so the next line does not start until this one ends.
                await asyncio.to_thread(_playback_module().play, job.path, True)
            except Exception as error:  # noqa: BLE001 - one bad line must not kill the consumer
                _status(f"Playback failed: {error}", True)
            finally:
                job.done.set()
                self._queue.task_done()

    def _enqueue(self, job):
        """Queue one rendered job, dropping the oldest pending one if full.

        Non-blocking: awaiting `put` would park a render task holding nothing
        useful. The dropped job keeps its index line and its audio, so it is
        still reachable through `replay`.
        """
        self._ensure_consumer()
        if self._queue.full():
            dropped = self._queue.get_nowait()
            self._queue.task_done()
            # Mandatory: a `wait: true` client whose job was dropped would
            # otherwise never get its reply.
            dropped.done.set()
            _status(f"Queue full: dropped history entry {dropped.entry_id}.")
        self._queue.put_nowait(job)

    def _drain(self):
        """Discard every pending job. Returns how many were discarded."""
        discarded = 0
        while True:
            try:
                job = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return discarded
            self._queue.task_done()
            job.done.set()
            discarded += 1

    # -- dedupe ------------------------------------------------------------

    def _is_duplicate(self, key):
        """True when this key was already accepted. Records it when it is new."""
        if not key:
            return False  # `kokoro-ttsd say` and build notifiers may repeat freely
        if key in self._seen_keys:
            return True
        self._seen_keys[key] = None
        while len(self._seen_keys) > DEDUPE_KEYS:
            self._seen_keys.popitem(last=False)
        return False

    # -- the protocol ------------------------------------------------------

    def _handle_request(self, request):
        """Map one decoded request to a reply, scheduling work where needed.

        Synchronous on purpose. There is no await between the duplicate test,
        the insert and the id reservation, so that sequence is atomic against
        every other client without a lock.
        """
        command = request.get("command", "speak")
        if command == "ping":
            count, size = self.store.counts()
            return {"ok": True, "pid": os.getpid(), "voice": self.voice, "lang": self.lang,
                    "format": self.format, "held": self.store.held(),
                    "queued": self._queue.qsize(), "history_count": count,
                    "history_bytes": size}
        if command == "stop":
            # Draining is the point: without it, cancelling the current line
            # would immediately start the next pending one.
            _playback_module().stop()
            self._drain()
            return {"ok": True}
        if command == "shutdown":
            self._shutdown.set()
            return {"ok": True}
        if command == "history":
            return self._handle_history(request)
        if command == "replay":
            return self._handle_replay(request)
        if command in ("hold", "release"):
            return {"ok": True, "held": self.store.set_held(command == "hold")}
        if command == "purge":
            # Stop first: deleting a file a queued job points at would leave
            # the consumer playing a path that is no longer there.
            _playback_module().stop()
            self._drain()
            removed, size = self.store.purge()
            return {"ok": True, "removed": removed, "bytes": size}
        if command != "speak":
            return {"ok": False, "error": f"Unknown command: {command}"}
        return self._handle_speak(request)

    def _handle_history(self, request):
        limit = request.get("limit", HISTORY_LIMIT_DEFAULT)
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            return {"ok": False, "error": "limit must be a positive integer."}
        return {"ok": True, "entries": self.store.entries(limit)}

    def _handle_replay(self, request):
        entry_id = request.get("id")
        if entry_id is None or entry_id == "":
            return {"ok": False, "error": "replay requires an id."}
        entry = self.store.find(entry_id)
        if entry is None:
            return {"ok": False, "error": f"No history entry {entry_id}."}
        path = self.store.path_for(entry["id"], entry["format"])
        if not os.path.exists(path):
            return {"ok": False, "error": f"Audio for {entry_id} is gone."}
        # No synthesis, no dedupe, no id reservation, and the hold marker is
        # ignored: replay is an explicit user action, and refusing it while
        # held would leave no way to hear the line the user held it for.
        self._enqueue(_Job(
            entry_id=str(entry["id"]),
            text=entry.get("text") or "",
            voice=entry.get("voice") or self.voice,
            speed=self.speed,
            lang=self.lang,
            fmt=entry["format"],
            path=path,
        ))
        return {"ok": True}

    def _handle_speak(self, request):
        text = (request.get("text") or "").strip()
        if not text:
            return {"ok": False, "error": "Request has no text."}
        if len(text) > MAX_TEXT_CHARS:
            text = text[:MAX_TEXT_CHARS]

        # Validated before an id is reserved and before any file is named, so
        # a bad format cannot leave an orphan behind for `sweep()` to clean up.
        fmt = request.get("format") or self.format
        if fmt not in ACCEPTED_FORMATS:
            return {"ok": False,
                    "error": f"Unknown format: {fmt}. Use one of: wav, ogg, mp3."}

        key = request.get("key")
        replace = bool(request.get("replace"))
        if replace:
            # A caller passing `replace` is deliberately repeating itself: a
            # voice sample and a build notifier both resend the same text. The
            # key is still recorded, so a later plain send of it is suppressed.
            self._is_duplicate(key)
        elif self._is_duplicate(key):
            return {"ok": True, "deduped": True}

        entry_id = self.store.next_id()
        held = self.store.held()
        job = _Job(
            entry_id=entry_id,
            text=text,
            voice=request.get("voice") or self.voice,
            speed=request.get("speed") or self.speed,
            lang=request.get("lang") or self.lang,
            fmt=fmt,
            path=self.store.path_for(entry_id, fmt),
            held=held,
            replace=replace,
        )
        asyncio.get_running_loop().create_task(self._render_quietly(job))
        if held:
            # Synthesized and kept, but not played. Nothing to wait for.
            return {"ok": True, "held": True, "id": entry_id}
        reply = {"ok": True, "queued": True, "id": entry_id}
        if request.get("wait"):
            return reply, job.done
        return reply

    async def handle_client(self, reader, writer):
        try:
            line = await reader.readline()
            if not line:
                return
            try:
                request = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                reply = {"ok": False, "error": f"Invalid JSON request: {error}"}
            else:
                if not isinstance(request, dict):
                    reply = {"ok": False, "error": "Request must be a JSON object."}
                else:
                    reply = self._handle_request(request)
                    if isinstance(reply, tuple):
                        # `wait: true`: the same reply, sent once the line has
                        # finished playing, been dropped, or failed to render.
                        reply, finished = reply
                        await finished.wait()
            writer.write((json.dumps(reply) + "\n").encode("utf-8"))
            await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            writer.close()

    async def serve(self, path):
        server = await asyncio.start_unix_server(self.handle_client, path=path)
        os.chmod(path, 0o600)
        _status(f"Listening on {path}")
        async with server:
            await self._shutdown.wait()
        _status("Shutting down.")


def _clear_stale_socket(path):
    """Remove a socket file left behind by a daemon that did not exit cleanly.

    Refuses to start when something is still listening, so two daemons never
    fight over the same path.
    """
    if not os.path.exists(path):
        return
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(1.0)
    try:
        probe.connect(path)
    except OSError:
        os.unlink(path)
    else:
        raise SystemExit(f"error: a daemon is already listening on {path}")
    finally:
        probe.close()


def request(payload, path=None, timeout=5.0, read_reply=True):
    """Send one request to a running daemon and return its decoded reply.

    Shared with the CLI subcommands below and usable as a library call. Callers
    that only need to fire a line off can pass `read_reply=False`.
    """
    resolved = socket_path(path)
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    try:
        client.connect(resolved)
        client.sendall((json.dumps(payload) + "\n").encode("utf-8"))
        if not read_reply:
            return {"ok": True}
        buffer = b""
        while not buffer.endswith(b"\n"):
            chunk = client.recv(4096)
            if not chunk:
                break
            buffer += chunk
        return json.loads(buffer.decode("utf-8")) if buffer.strip() else {"ok": False, "error": "Empty reply."}
    finally:
        client.close()


async def _serve(args):
    path = socket_path(args.socket)
    _clear_stale_socket(path)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    daemon = SynthesisDaemon(
        voice=args.voice, lang=args.language, speed=args.speed, fmt=args.format
    )
    try:
        await daemon.start()
        await daemon.serve(path)
    finally:
        daemon.close()
        if os.path.exists(path):
            os.unlink(path)


def _client_command(payload, args):
    try:
        reply = request(payload, path=args.socket, timeout=args.timeout)
    except OSError as error:
        print(f"error: cannot reach the daemon on {socket_path(args.socket)}: {error}", file=sys.stderr)
        return 1
    print(json.dumps(reply))
    return 0 if reply.get("ok") else 1


def build_parser():
    parser = argparse.ArgumentParser(
        prog="kokoro-ttsd",
        description="Keep one Kokoro pipeline warm and speak lines sent to its socket.",
    )
    subparsers = parser.add_subparsers(dest="command")
    socket_help = "Unix socket path. Defaults to $KOKORO_TTS_SOCKET, then paths.DAEMON_SOCKET."

    serve = subparsers.add_parser("serve", help="Run the daemon in the foreground.")
    serve.add_argument("--voice", default=os.environ.get("KOKORO_DAEMON_VOICE", DEFAULT_VOICE))
    serve.add_argument("--language", default=os.environ.get("KOKORO_DAEMON_LANG", DEFAULT_LANG))
    serve.add_argument("--speed", type=float, default=float(os.environ.get("KOKORO_DAEMON_SPEED", DEFAULT_SPEED)))
    serve.add_argument(
        "--format",
        choices=ACCEPTED_FORMATS,
        default=os.environ.get("KOKORO_DAEMON_FORMAT", DEFAULT_FORMAT),
        help="Container for stored audio. Defaults to $KOKORO_DAEMON_FORMAT, then ogg.",
    )
    serve.add_argument("--socket", help=socket_help)

    say = subparsers.add_parser("say", help="Send one line to a running daemon.")
    say.add_argument("text", help="Text to speak.")
    say.add_argument("--voice")
    say.add_argument("--speed", type=float)
    say.add_argument("--wait", action="store_true", help="Block until the line finishes playing.")
    say.add_argument("--format", choices=ACCEPTED_FORMATS, help="Override the daemon's audio format for this line.")
    say.add_argument("--key", help="Dedupe key. A repeat of a key the daemon has seen is ignored.")
    say.add_argument("--replace", action="store_true", help="Interrupt playback and drop pending lines instead of queueing.")
    say.add_argument("--socket", help=socket_help)
    say.add_argument("--timeout", type=float, default=60.0)

    for name, help_text in (
        ("ping", "Check that a daemon is running."),
        ("stop", "Stop playback and drop pending lines."),
        ("shutdown", "Ask the daemon to exit."),
        ("hold", "Stop playing new lines. They are still synthesized and kept."),
        ("release", "Resume playing new lines. Held lines are not replayed."),
        ("purge", "Delete every stored audio file."),
    ):
        control = subparsers.add_parser(name, help=help_text)
        control.add_argument("--socket", help=socket_help)
        control.add_argument("--timeout", type=float, default=5.0)

    history = subparsers.add_parser("history", help="List stored lines, newest first.")
    history.add_argument("--limit", type=int, default=HISTORY_LIMIT_DEFAULT)
    history.add_argument("--socket", help=socket_help)
    history.add_argument("--timeout", type=float, default=5.0)

    replay = subparsers.add_parser("replay", help="Play a stored line again by id.")
    replay.add_argument("id", help="History entry id, as `kokoro-ttsd history` prints it.")
    replay.add_argument("--socket", help=socket_help)
    replay.add_argument("--timeout", type=float, default=5.0)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command or "serve"
    if command == "serve":
        for attribute, default in (
            ("socket", None),
            ("voice", DEFAULT_VOICE),
            ("language", DEFAULT_LANG),
            ("speed", DEFAULT_SPEED),
            ("format", DEFAULT_FORMAT),
        ):
            if not hasattr(args, attribute):
                setattr(args, attribute, default)
        try:
            asyncio.run(_serve(args))
        except KeyboardInterrupt:
            return 0
        return 0
    if command == "say":
        payload = {"command": "speak", "text": args.text, "wait": args.wait}
        if args.voice:
            payload["voice"] = args.voice
        if args.speed:
            payload["speed"] = args.speed
        # Only when set, so an older daemon sees the payload it sees today.
        if args.format:
            payload["format"] = args.format
        if args.key:
            payload["key"] = args.key
        if args.replace:
            payload["replace"] = True
        return _client_command(payload, args)
    if command == "history":
        return _client_command({"command": "history", "limit": args.limit}, args)
    if command == "replay":
        return _client_command({"command": "replay", "id": args.id}, args)
    return _client_command({"command": command}, args)


if __name__ == "__main__":
    sys.exit(main())
