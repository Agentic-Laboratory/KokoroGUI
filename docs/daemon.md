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
| `command` | `speak` (the default), `ping`, `stop`, `shutdown`, `history`, `replay`, `hold`, `release`, `purge`. |
| `text` | The line to speak, for `speak`. The daemon accepts up to 2000 characters, but `kokoro_engine.py`'s preview writer truncates each segment to 500 characters and keeps only the first two, so the effective spoken length is about 500 characters regardless of the daemon's own cap; see [claude-code.md](claude-code.md#the-fallback-ladder) for why that matters to callers speaking model-authored prose. Longer input belongs in `kokoro-tts synthesize`, not this socket. |
| `voice`, `speed`, `lang` | Override the daemon's defaults for this line. |
| `format` | `wav`, `ogg`, or `mp3`, for this `speak` request only. Omitted uses the daemon's own default (`ogg` unless `serve --format` says otherwise). |
| `key` | Dedupe key, for `speak`. A repeat of a key already seen is ignored and answered `{"ok": true, "deduped": true}`, unless `replace` is set. |
| `replace` | For `speak`. Stops whatever is playing, drops every pending line, and bypasses the dedupe check, so this line queues immediately. |
| `wait` | When true, the reply is withheld until the line has finished playing, or, for a request that was deduped or held, returned immediately, since there is nothing to wait for. |
| `limit` | For `history`. How many entries to return, newest first. Defaults to 15. |
| `id` | For `replay`. Which stored entry to play again. |

Replies always carry `ok`. A rejected request carries `error` instead of the fields below.

| Reply shape | When |
|---|---|
| `{"ok": true, "queued": true, "id": "0042"}` | A `speak` request was accepted: synthesized, recorded to history, and queued for, or already at, playback. |
| `{"ok": true, "deduped": true}` | The `key` on a `speak` request had already been seen. Nothing was synthesized or queued. |
| `{"ok": true, "held": true, "id": "0042"}` | A `speak` request was accepted while hold is engaged: synthesized and recorded to history, but not queued for playback. |
| `{"ok": true, "entries": [...]}` | Reply to `history`. Entries newest first, each carrying `id`, `text`, `voice`, `format`, `bytes`, `created`. |
| `{"ok": true}` | Reply to `replay`, `stop`, or `shutdown`. |
| `{"ok": true, "held": true\|false}` | Reply to `hold` or `release`. |
| `{"ok": true, "removed": 12, "bytes": 290100}` | Reply to `purge`. |

`ping` carries its own shape:

| Field | Meaning |
|---|---|
| `pid` | Process id of the running daemon. |
| `voice`, `lang` | The daemon's current defaults. |
| `format` | The daemon's current default output format. |
| `held` | Whether hold is currently engaged. |
| `queued` | Pending lines waiting to play; does not count the one currently playing. |
| `history_count`, `history_bytes` | Size of the stored history, for a `ping` caller that wants to show it without a separate `history` call. |

## Interruption

Synthesis is serialized behind one lock, because a single `KPipeline` is not safe to drive concurrently. Playback runs outside that lock, but it no longer replaces whatever is already playing: a `speak` request queues behind whatever is pending, and lines always play one at a time, strictly in arrival order, never overlapping, so several Claude Code sessions sharing one daemon never talk over each other. The pending queue holds up to 100 lines; that cap is a runaway safety valve rather than a routine limit, so normal use should never come close to it. Once the cap is reached, the queue drops the **oldest** pending line, not the newest, so a burst of requests plays out in the order it arrived rather than jumping to whichever line showed up last. A dropped line is not lost: every line is written to history before this decision is made, so it stays recoverable with `kokoro-ttsd replay`. `replace: true` restores the old newest-wins behavior for a caller that wants an immediate interrupt, such as a build notifier: it stops whatever is playing, drops every pending line, and queues just this one.

## Cost

A warm daemon holds the model resident: about 1.2 GB. That is the trade for the latency. Where that is too much to keep loaded all the time, run the daemon only for the sessions that want spoken output, or call `kokoro-tts` and accept the cold start.

History adds a second, much smaller cost. Up to 200 entries are kept by default, oldest pruned first, and the running total is reported in `ping`'s `history_bytes` field. At the default Ogg Vorbis format, 200 lines land around a megabyte, nowhere near the model's memory footprint, but it is a new resource this daemon now owns, and it lives under the system temporary directory (`tempfile.gettempdir()`), never inside this repository. Treat it as a recent-session convenience rather than an archive: macOS clears items under the system temporary directory after roughly three days of disuse.

## Running it at login on macOS

Use a LaunchAgent that runs `kokoro-ttsd serve` with `RunAtLoad`, and `KeepAlive` set to `SuccessfulExit: false` so a crash restarts but a deliberate `kokoro-ttsd shutdown` stays down. The agent must run in the GUI session (`launchctl bootstrap gui/$UID ...`) to reach an audio device.
