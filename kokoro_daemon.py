"""Long-lived synthesis service for callers that cannot pay a cold start.

Importing torch and building a KPipeline costs about ten seconds, and
`kokoro-tts` pays that on every invocation. A caller that wants a spoken line
the moment something finishes - an editor hook, a build notifier - cannot use
the CLI for that reason alone.

The daemon pays the cost once at startup and then answers newline-delimited
JSON requests over a Unix domain socket, so a request speaks in about a second.
It is a third front end over `KokoroEngine`, alongside `gui.py` and
`kokoro_cli.py`, and owns no synthesis logic of its own.

Synthesis is serialized behind one lock because a single KPipeline is not safe
to drive concurrently. Playback deliberately runs outside that lock: `playback`
already replaces whatever is playing, so a fresh request interrupts an older
line instead of queueing behind it.
"""

import argparse
import asyncio
import json
import os
import socket
import sys
import tempfile
from pathlib import Path

from paths import DAEMON_SOCKET

# Resolved lazily for the same reason kokoro_cli does it: importing the engine
# pulls in torch, which would turn `kokoro-ttsd ping` from instant into ~3s.
KokoroEngine = None
playback = None

DEFAULT_VOICE = "af_heart"
DEFAULT_LANG = "a"
DEFAULT_SPEED = 1.0

# A spoken status line is a sentence or two. Anything longer is a document, and
# the caller should use `kokoro-tts synthesize` instead of a notification path.
MAX_TEXT_CHARS = 2000


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


class SynthesisDaemon:
    """Holds one warm engine and speaks the text that arrives on the socket."""

    def __init__(self, voice=DEFAULT_VOICE, lang=DEFAULT_LANG, speed=DEFAULT_SPEED):
        self.voice = voice
        self.lang = lang
        self.speed = speed
        self.engine = None
        self._synth_lock = asyncio.Lock()
        self._request_id = 0
        self._shutdown = asyncio.Event()

    async def start(self):
        """Load the engine and the pipeline. Everything after this is warm."""
        self.engine = _engine_class()()
        self.engine.on_status = lambda message, is_error: _status(message, is_error)
        if not await self.engine.init_pipeline_async(self.lang):
            raise RuntimeError(f"Pipeline initialization failed for language '{self.lang}'.")
        # Force the first inference here rather than making the first real
        # request pay for lazy weight loading inside the pipeline. Synthesized
        # and discarded, never played: startup should be silent.
        warmup = await self._synthesize("Ready.", self.voice, self.speed, self.lang)
        if warmup:
            Path(warmup).unlink(missing_ok=True)

    def close(self):
        if self.engine is not None:
            self.engine.worker.stop()
            self.engine = None

    async def _synthesize(self, text, voice, speed, lang):
        """Render one line to a temporary WAV and return its path, or None."""
        handle, path = tempfile.mkstemp(prefix="kokoro-daemon-", suffix=".wav")
        os.close(handle)
        config = {"lexicon": {}, "normalize": True, "trim_silence": True}
        async with self._synth_lock:
            try:
                ok = await self.engine.generate_preview(
                    text, voice, speed, path, config, lang_code=lang
                )
            except Exception as error:  # noqa: BLE001 - one bad line must not kill the daemon
                _status(f"Synthesis failed: {error}", True)
                ok = False
        if not ok:
            Path(path).unlink(missing_ok=True)
            return None
        return path

    async def speak(self, text, voice=None, speed=None, lang=None):
        """Synthesize and play one line, superseding whatever is still playing."""
        self._request_id += 1
        request_id = self._request_id
        path = await self._synthesize(
            text, voice or self.voice, speed or self.speed, lang or self.lang
        )
        if path is None:
            return False
        try:
            # A request that arrived while this one was synthesizing wins: drop
            # this audio rather than speaking a line the caller has moved past.
            if request_id != self._request_id:
                return False
            await asyncio.to_thread(_playback_module().play, path, True)
            return True
        finally:
            Path(path).unlink(missing_ok=True)

    def _handle_request(self, request):
        """Map one decoded request to a reply, scheduling work where needed."""
        command = request.get("command", "speak")
        if command == "ping":
            return {"ok": True, "pid": os.getpid(), "voice": self.voice, "lang": self.lang}
        if command == "stop":
            _playback_module().stop()
            return {"ok": True}
        if command == "shutdown":
            self._shutdown.set()
            return {"ok": True}
        if command != "speak":
            return {"ok": False, "error": f"Unknown command: {command}"}

        text = (request.get("text") or "").strip()
        if not text:
            return {"ok": False, "error": "Request has no text."}
        if len(text) > MAX_TEXT_CHARS:
            text = text[:MAX_TEXT_CHARS]
        coroutine = self.speak(
            text, request.get("voice"), request.get("speed"), request.get("lang")
        )
        if request.get("wait"):
            return coroutine
        # Fire and forget: the caller is a hook that must not wait ~1s for audio.
        asyncio.get_running_loop().create_task(self._speak_quietly(coroutine))
        return {"ok": True, "queued": True}

    async def _speak_quietly(self, coroutine):
        try:
            await coroutine
        except Exception as error:  # noqa: BLE001 - a background task must not take the loop down
            _status(f"Playback failed: {error}", True)

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
                    if asyncio.iscoroutine(reply):
                        reply = {"ok": await reply}
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
    daemon = SynthesisDaemon(voice=args.voice, lang=args.language, speed=args.speed)
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
    serve.add_argument("--socket", help=socket_help)

    say = subparsers.add_parser("say", help="Send one line to a running daemon.")
    say.add_argument("text", help="Text to speak.")
    say.add_argument("--voice")
    say.add_argument("--speed", type=float)
    say.add_argument("--wait", action="store_true", help="Block until the line finishes playing.")
    say.add_argument("--socket", help=socket_help)
    say.add_argument("--timeout", type=float, default=60.0)

    for name, help_text in (("ping", "Check that a daemon is running."), ("stop", "Stop playback."), ("shutdown", "Ask the daemon to exit.")):
        control = subparsers.add_parser(name, help=help_text)
        control.add_argument("--socket", help=socket_help)
        control.add_argument("--timeout", type=float, default=5.0)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command or "serve"
    if command == "serve":
        for attribute, default in (("socket", None), ("voice", DEFAULT_VOICE), ("language", DEFAULT_LANG), ("speed", DEFAULT_SPEED)):
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
        return _client_command(payload, args)
    return _client_command({"command": command}, args)


if __name__ == "__main__":
    sys.exit(main())
