# Kokoro TTS Daemon

`kokoro-ttsd` keeps one Kokoro pipeline warm and speaks lines sent to a Unix socket. It exists for callers that cannot pay a cold start: importing torch and building a `KPipeline` costs about ten seconds, and `kokoro-tts` pays that on every invocation. A notifier that wants to speak the moment a build, a test run, or an editor session finishes cannot use the CLI for that reason alone.

The daemon is a third front end over `KokoroEngine`, alongside `gui.py` and `kokoro_cli.py`. It owns no synthesis logic of its own.

## Install

The daemon ships with the package; `pip install -e .` puts `kokoro-ttsd` on the path next to `kokoro-tts`. It needs a working audio device, unlike standard CLI synthesis.

## Run it

```bash
kokoro-ttsd serve
```

Startup loads the pipeline and synthesizes one line that is discarded rather than played, so the first real request does not pay for lazy weight loading. Expect the socket to accept connections a few seconds after launch; `kokoro-ttsd ping` is the readiness check.

Then, from anywhere:

```bash
kokoro-ttsd say "The build passed."
```

That returns in well under a tenth of a second. The daemon acknowledges the request and synthesizes afterwards, so a caller on a hot path never waits for audio. Pass `--wait` when you do want the call to block until the line has finished playing.

| Command | Purpose |
|---|---|
| `serve` | Run the daemon in the foreground. The default when no command is given. |
| `say TEXT` | Speak one line. `--voice`, `--speed`, `--wait`. |
| `ping` | Report the pid, voice, and language of a running daemon. |
| `stop` | Stop whatever is playing. |
| `shutdown` | Ask the daemon to exit. |

`serve` takes `--voice`, `--language`, and `--speed` for the defaults every request inherits; `$KOKORO_DAEMON_VOICE`, `$KOKORO_DAEMON_LANG`, and `$KOKORO_DAEMON_SPEED` set the same things.

## The socket

The socket path is `--socket`, then `$KOKORO_TTS_SOCKET`, then `daemon.sock` beside the source files (`paths.DAEMON_SOCKET`), which is where the GUI and CLI keep the rest of the application's state. It is created with mode `0600`.

`--socket` belongs after the command — `kokoro-ttsd ping --socket PATH`, not `kokoro-ttsd --socket PATH ping`.

A socket file left behind by a daemon that did not exit cleanly is removed at startup. A socket with something still listening makes startup refuse and exit non-zero, so two daemons never fight over one path. Shut down a manually started daemon (`kokoro-ttsd shutdown`) before loading a service that starts another one, or the supervisor will restart it into that refusal on a loop.

## Protocol

One JSON object per connection, newline-terminated, answered with one JSON line. Any language with a Unix socket can drive it.

```bash
printf '{"command":"speak","text":"Hello."}\n' | nc -U daemon.sock
```

Requests:

| Field | Meaning |
|---|---|
| `command` | `speak` (the default), `ping`, `stop`, `shutdown`. |
| `text` | The line to speak. Truncated at 2000 characters: longer input is a document, and belongs in `kokoro-tts synthesize`. |
| `voice`, `speed`, `lang` | Override the daemon's defaults for this line. |
| `wait` | When true, the reply is withheld until the line has finished playing. |

Replies always carry `ok`. A queued line answers `{"ok": true, "queued": true}`; a rejected one carries `error`.

## Interruption

Synthesis is serialized behind one lock, because a single `KPipeline` is not safe to drive concurrently. Playback deliberately runs outside that lock: `playback.play` already replaces whatever is playing, so a new request interrupts an older line rather than queueing behind it. A request that is superseded while it is still synthesizing is dropped without playing, on the grounds that the caller has moved past it.

## Cost

A warm daemon holds the model resident: about 1.2 GB. That is the trade for the latency. Where that is too much to keep loaded all the time, run the daemon only for the sessions that want spoken output, or call `kokoro-tts` and accept the cold start.

## Running it at login on macOS

Use a LaunchAgent that runs `kokoro-ttsd serve` with `RunAtLoad`, and `KeepAlive` set to `SuccessfulExit: false` so a crash restarts but a deliberate `kokoro-ttsd shutdown` stays down. The agent must run in the GUI session (`launchctl bootstrap gui/$UID ...`) to reach an audio device.
