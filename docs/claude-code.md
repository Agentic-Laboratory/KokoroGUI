# Claude Code Integration

Claude Code speaks selected assistant messages through the same daemon the CLI, GUI and menu bar already use: `kokoro-ttsd`. The hook that decides what to speak, and its configuration, live in a different repository: read `~/Code/dot-files/claude-code/docs/spoken-bottom-line.md` for the client side (the `MessageDisplay` hook itself, its config keys, and how to turn it on) before continuing here. This page documents the daemon-side behavior that hook drives, and the protocol contract between them.

## What Claude Code sends

Claude Code's `MessageDisplay` hook event fires once per completed batch of assistant output lines, not once per turn and not once per message. A single assistant reply that streams in over several seconds can arrive as many separate hook invocations, each carrying the same `message_id` and a growing `index`. A 24-flush message, one logical reply delivered as flushes 0 through 23 with `final` true only on the last, has been observed on a live capture; it is not a hypothetical edge case. This is exactly why dedupe has to live in the daemon rather than in the hook: one logical message becomes several `speak` requests sharing one `message_id`, and something has to collapse them back into a single spoken line. See [Dedupe and the speak queue](#dedupe-and-the-speak-queue).

## Protocol fields the hook uses

The hook's `speak` request is a subset of the full protocol. It sets:

| Field | Sent when |
|---|---|
| `text` | Always: the marker line, or the closing-paragraph fallback, prefixed with the source label described below. |
| `key` | Always, once the message carries a `message_id`. This is the daemon's dedupe key. |
| `format` | When the hook's own config names one; otherwise omitted, and the daemon's own default applies. |
| `wait` | Never, from this hook; `wait` and `replace` both take their protocol default of `false`, so a spoken line queues behind whatever is already pending instead of interrupting it. `--test` and other manual invocations of the hook may still set `wait`. |

The full request and reply shapes, including `voice`, `speed`, `lang`, `replace`, and every reply variant, are the canonical protocol table in [daemon.md#protocol](daemon.md#protocol); this page does not repeat it.

## The source label

Several Claude Code sessions share one daemon, and the daemon owns the only speaker, so every line it plays sounds identical in origin. The hook therefore prefixes the text it sends with where the line came from: the repository derived from the session's working directory, then a NATO alphabet word hashed from the session id, as in `KokoroGUI, delta: the four gates pass.` This is entirely a hook-side decision about the `text` field. Nothing in the daemon inspects it, and nothing about it changes the dedupe contract: suppression keys on the `key` field, which stays the bare `message_id`, so a labelled line and an unlabelled one with the same id are still the same message. The hook's `announce_source` key turns it off, and the label is added after the hook's own `max_chars` cap, so it is the verdict that loses its tail and never the attribution. Ahead of the label, the hook also puts the date and time it sent the line, as in `September 23, 12:47 PM. KokoroGUI, delta: the four gates pass.`; its `announce_time` key turns that off. The daemon passes it through like any other text, so it ends up in the stored audio and a replayed or held line still says when it was written.

Two things follow for the daemon. The label consumes characters against the roughly 500 the preview writer actually speaks, so a long closing paragraph loses a little more of its tail than before. And the label is now the text that sits in front of the speaker-directive scan: that scan matches `[Name]:` or `[Name:FX]:` anywhere in the line, not only at its start, and it discards everything before the first match, so a verdict quoting a bracketed tag or a Markdown link loses the label along with the prose that preceded it. The label itself is safe, since `KokoroGUI, delta:` has no brackets to match. The dropped-prefix behavior predates this integration and is out of scope to change here; it now costs the attribution as well as the opening words.

## The marker contract

The output style asks the model to end a qualifying message with a single line starting `Exec summary:` (the older `Bottom line:` is still recognized). It must be one line, never a heading with a body underneath. `MessageDisplay` delivers whole completed lines in batches, so a single-line marker is always wholly contained in one flush, and the hook can regex-match it the instant that flush arrives. A heading with the verdict written below it would span two or more flushes, forcing the hook to buffer every message until `final: true` before it could speak anything at all, which throws away the entire latency advantage of the per-message event.

## The fallback ladder

| Case | What is spoken |
|---|---|
| Marker present | The marker line itself, from whichever flush it appeared in. |
| No marker, message is long enough (`min_lines`, default 5) | The closing paragraph, decided once the message reaches `final: true`. |
| No marker, message is short | Nothing. No socket connection is opened at all. |

When a message discusses the label before actually using it, the last matching line wins, not the first. The fallback reads the closing paragraph rather than the opening one because the output style is verdict-last by construction: the sentence a reader wants is the one at the end of the message, not the preamble that opens an agentic turn ("I'll start by checking the config").

A closing paragraph read by the fallback is still subject to the daemon's own effective length limit: `kokoro_engine.py`'s preview writer speaks roughly the first 500 characters of whatever text it is given, not the full 2000 characters the daemon's `text` field accepts (see [daemon.md#protocol](daemon.md#protocol)). Separately, that same writer treats any `[Text]:` anywhere in the line as a speaker directive, so an exec summary that quotes a bracketed tag or a Markdown link can come out mangled, with the bracketed text stripped and a voice lookup attempted against it. Both behaviors predate this integration and are out of scope to change here; they just matter more now that the daemon is fed far more model-authored prose than it used to be.

## Dedupe and the speak queue

Dedupe is keyed on `message_id` and lives entirely in the daemon. The daemon is single-threaded asyncio, and its request handler performs the membership check, the insert, and the id reservation with no `await` in between, so the whole sequence is atomic against every other client without needing a lock. That property does not hold on the hook side: `MessageDisplay` runs as a separate short-lived process per flush, the harness permits several of those processes to be in flight at once for a single message, and a second Claude Code session sharing the same daemon socket can interleave its own flushes mid-stream. Any hook-side memory of what was already spoken would have to live in a file that multiple processes read, compare and rewrite with nothing to arbitrate the race, so the daemon is the only place dedupe can safely live.

One line is spoken at a time. The daemon guarantees that structurally: one socket, one consumer, playback that blocks until the audio ends, so concurrent sessions queue instead of overlapping. The hook's `say` fallback, which takes over when the daemon is unreachable, holds an exclusive `flock` in its spool directory for the duration of each `say` so that it makes the same promise; see `spoken-bottom-line.md` for how that child process works.

Requests queue FIFO, capped at 100 pending, rather than the old newest-wins interrupt. Lines always play one at a time in arrival order: a single consumer awaits each line's playback before starting the next, so two sessions finishing a turn together are heard in sequence, never on top of each other. The cap is a runaway safety valve rather than a routine limit. On overflow the queue drops the **oldest** pending request, not the newest, so a burst of messages plays out in the order it was sent rather than jumping straight to whatever arrived last. `replace: true` restores the old interrupt-and-replace behavior for a caller that wants it, such as a build notifier or the menu bar's own voice-sample button; it also bypasses the dedupe check, since a caller passing `replace` is deliberately repeating text it may already have sent. `purge` clears stored history but does **not** clear the dedupe key set: it is a cleanup of stored audio, not a reset of what the daemon has already accepted.

## History and hold

Every line the daemon synthesizes is written to history before the play-or-hold decision is made. A line that is held, superseded by a `replace`, or dropped by queue overflow is still on disk and still recoverable through `history` and `replay`; only a line whose synthesis failed outright is missing from it.

Hold is a marker file on disk, not an in-memory flag, so a daemon restarted by a LaunchAgent comes back still held rather than silently resuming playback. `release` removes the marker and nothing else: it does not replay the backlog that queued up while held. That is deliberate. Hold exists so a pile of messages that accumulated while someone stepped away does not burst-play at them the moment they return; releasing and then hearing eleven queued summaries in a row is exactly the failure hold is meant to prevent. The held lines are not lost, they sit in history with their audio intact, reachable through the menu bar's History submenu or `kokoro-ttsd replay`.

The history store lives under the system temporary directory (`tempfile.gettempdir()`), never inside this repository. Treat it as a recent-session convenience, not an archive: macOS clears items under the system temporary directory after roughly three days of disuse, so history from an idle machine may already be gone by the time someone goes looking for it.

## Audio format and the Linux constraint

The daemon's default output format is Ogg Vorbis, configurable per line (the `format` field on a `speak` request) and per daemon instance (`kokoro-ttsd serve --format`). Encoding happens entirely inside `kokoro_engine.py`'s existing extension-driven write path (`pedalboard.io.AudioFile`, falling back to `soundfile.write`); both already select their encoder from the output file's extension, so nothing in the engine needed to change to support this.

Linux's `aplay` backend in `playback.py` decodes WAV, AU and VOC only; it does not decode Ogg Vorbis or MP3. Non-WAV formats must never be routed to `aplay` there. On Linux, anything other than a `.wav` path falls through to the `sounddevice` playback path instead.

## Verifying the chain end to end

```bash
kokoro-ttsd ping
python3 ~/.claude/hooks/speak-bottom-line.py --check
python3 ~/.claude/hooks/speak-bottom-line.py --test "Exec summary: verifying the chain."
kokoro-ttsd history --limit 5
kokoro-ttsd replay <id>      # the id kokoro-ttsd history just printed
kokoro-ttsd hold
kokoro-ttsd release
kokoro-ttsd purge
```

`ping` confirms the daemon is up and reports its format, hold state, queue depth, and history size. The hook's own `--check` reports its resolved configuration and which events it is wired to (see [When nothing is spoken](#when-nothing-is-spoken) below). `--test` sends a line without waiting for a real turn; it sends no `key`, so running it twice in a row never gets swallowed as a duplicate of itself, and it carries the repository half of the source label so the label is audible too. `history` should show the test line, and `replay` plays it back by the id `history` just printed. `hold` and `release` confirm the queue can be paused and resumed without losing anything, since a held line is still written to history. `purge` clears stored audio and the index, but it does not clear the dedupe key set: a `key` the daemon has already accepted stays deduped even after a purge.

## When nothing is spoken

| Symptom | Check |
|---|---|
| Config `enabled` is `false` | The hook's `--check` reports the resolved `enabled` value. |
| `scope: "turn"` and the message is not the last one of the turn | Expected. Under `scope: "turn"`, only the `Stop`-triggered flush at the end of the turn sends; `MessageDisplay` is retained and still wired, but under this scope it only spools. |
| Message is under `min_lines` with no marker | The fallback ladder's third rung: short and unmarked stays silent by design. |
| Hold is engaged | `kokoro-ttsd ping` reports `held: true`. Release it, or look the line up in history instead. |
| Daemon is down | `kokoro-ttsd ping` fails to connect. The hook's `fallback` setting decides what happens next; under `say` the voice changes and concurrent sessions still speak one at a time. |
| Queue is full | Five lines were already pending; the oldest of those was dropped, not the new one. Check history for it. |

## See also

- [daemon.md](daemon.md), the canonical protocol reference.
- [menubar.md](menubar.md), for the History, Hold, Format and Clear history controls.
- `~/Code/dot-files/claude-code/docs/spoken-bottom-line.md`, for the hook itself and its configuration keys.
