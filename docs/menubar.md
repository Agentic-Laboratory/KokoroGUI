# Kokoro Menu Bar

`kokoro-ttsmenu` puts the [Kokoro TTS Daemon](daemon.md) in the menu bar: a status glyph at a glance, the state and voice one click away, and a few clicks to restart the daemon, stop whatever it is speaking, or pick a different voice, all without opening a terminal.

It runs as a separate process from the daemon, talking to it over the same Unix socket the CLI and the Claude Code hook already use. That separation is not incidental. AppKit needs to own the main thread to run a menu bar app, and the daemon needs asyncio's event loop on its own. Merging the two would mean the process holding a menu bar icon also has to hold the 1.2 GB model resident. `kokoro-ttsmenu` never imports the synthesis engine and never loads a model. It is a client, the same as `kokoro-tts` or the Claude Code hook, just with a UI instead of a command line.

## Install

Ships with the package on macOS. `pip install -e .` puts `kokoro-ttsmenu` on the path next to `kokoro-tts` and `kokoro-ttsd`. Its one extra dependency, `rumps` (the AppKit bindings), carries a `sys_platform == 'darwin'` marker in `pyproject.toml`, so a Linux or Windows install skips it. Entry points take no such marker, so `kokoro-ttsmenu` is still written to the path on those platforms; running it there fails with `ModuleNotFoundError: No module named 'AppKit'`. That is a macOS-only front end behaving as expected, not a broken install. The module itself imports cleanly everywhere, which is what lets the test suite run on the Linux and Windows CI runners.

## Run it

```bash
kokoro-ttsmenu
```

A status item appears in the menu bar; there is no window and no Dock icon. Quit from the menu. There is no window to close, and no keyboard shortcut takes its place.

Launching it does not start the daemon. If nothing is listening on the socket, the bar reads `○` and the status line at the top of the dropdown reads `Kokoro (not running, —)`, and both stay that way until you click **Restart daemon**. (See [What it will not do](#what-it-will-not-do) below for why.)

## The menu

| Item | Behavior |
|---|---|
| Status (disabled row at the top) | The state and current voice, for example `Kokoro (warm, af_sky)`. This is the whole status the bar title used to spell out; it lives here, not in the bar, because it is the widest custom item in a bar already split by the notch and crowded by other apps, and macOS drops an item outright rather than shrink it - see [Status glyphs](#status-glyphs) below for what stays in the bar itself. Not clickable, so it never highlights. |
| Speaking | Checkbox mirroring the config's `enabled` key. Clicking it writes the new value straight to the config. Hidden, along with Voice and Format, unless `~/.claude/speak-bottom-line.json` exists. |
| Voice | Submenu of every built-in voice, grouped by language the way `kokoro-tts voices` groups them. Selecting one writes `voice` to the config and speaks a one-line sample in that voice, so you hear the choice before trusting it. Custom voices added under `custom_voices/` do not appear here. |
| Format | Submenu of `wav` / `ogg` / `mp3`. Selecting one writes `format` to the config, inserting the key if the config predates this setting. Hidden, along with Speaking and Voice, unless the config file exists. |
| Hold output | Checkbox mirroring the daemon's hold state. While held, spoken lines are still synthesized and written to history, they are just not played; releasing does not replay whatever queued up while held. Shows the pending count once anything is waiting, for example `Hold output (3 queued)`. Greyed out unless the daemon is warm. |
| History | Submenu of the last 15 spoken lines, newest first, each labelled with a truncated single line of its text. Selecting one replays it. Reads `(no history)` when nothing has been spoken yet, or `(daemon not running)` when the daemon is cold. History lives under the system temporary directory, not this repository, and is a recent-session convenience rather than an archive: macOS clears items under the system temporary directory after roughly three days of disuse, so a long-idle machine's history may already be empty. |
| Clear history (N files, X.X MB) | Sends a purge request that deletes every stored line and its audio. Greyed out when history is empty or the daemon is not warm. |
| Stop speaking now | Sends `stop` to the daemon. Greyed out unless the daemon is warm: there is nothing to stop otherwise. |
| Restart daemon | Shuts the daemon down and starts it again. When nothing is running, this is also Start: a shutdown sent to a dead socket fails immediately, and the app moves straight to spawning, so there is no separate Start item to look for. It works against a daemon it did not start, including one launched by the LaunchAgent, because a shutdown request does not care who owns the process. Greyed out while a restart is already in flight. |
| Quit | Leaves the daemon running. Quitting the menu bar app has no effect on the socket or the process behind it. |

A restart can take a while on a first run, since a cold pipeline load downloads model weights; the bar stays on `◐` and the status row on `starting` for as long as the daemon process is alive, not just for a fixed grace period. If a spawn fails outright, an error line appears in the menu (for example, "daemon failed to start, see `~/Library/Logs/kokoro-ttsd.log`") and the bar drops to `○` (`not running` in the status row) immediately rather than waiting.

## Status glyphs

| Glyph | Status | Meaning |
|---|---|---|
| ● | warm | The daemon answered a ping with `ok`. Ready to speak immediately. |
| ◐ | starting | A daemon **this app spawned** is alive but has not answered a ping yet: still loading the pipeline. |
| ○ | not running | Nothing is listening on the socket. |

The `starting` state is only visible for a daemon the app started itself, because that is the only one whose process it holds a handle on. A daemon launched elsewhere, by the LaunchAgent or by hand, has no socket at all until its pipeline has finished loading, so it reads `not running` until the moment it answers its first ping. On a first run that downloads model weights, that can be several minutes of `not running` for a daemon that is in fact starting normally.

Hold is a separate, orthogonal indicator layered onto both: the bar appends a one-character `‖` mark to the glyph, for example `●‖`, and the status row appends `, held` to its wording, for example `Kokoro (warm, af_sky, held)`. Both appear whenever Hold output is engaged, regardless of which glyph is showing, since a daemon can be warm, starting, or stopped independently of whether it is letting audio through. The bar mark is deliberately one character rather than a word: it is already the widest custom item in a bar split by the notch and crowded on both sides, and macOS hides an item outright rather than shrink it, so spelling `, held` out in the bar itself would reopen the exact width problem the glyph-only title exists to avoid.

The menu bar pings the daemon every two seconds, on a worker thread, so a slow or hung reply never freezes the menu itself. Between ticks the bar, the status row, and the rest of the menu just show whatever the last successful check found.

## Configuration

Same file the Claude Code hook reads: `~/.claude/speak-bottom-line.json` by default, or the path in `$KOKORO_MENUBAR_CONFIG` when that variable is set to something non-empty. Point it at a scratch file to try the menu against a config that is not your real one.

Speaking, Voice and Format only appear once that file exists. There is nothing to toggle or pick if speaking was never configured. One gap follows from that: the hook also accepts an older, extensionless `~/.claude/speak-bottom-line` file as a bare on switch, and the menu bar does not read it. On a machine using only that legacy flag the hook will speak while the menu shows no Speaking control at all. Create the `.json` file (copy `claude-code/speak-bottom-line.json.example` from the dot-files repo) to get the controls back. If the file exists but cannot be parsed (bad JSON, or empty once comments are stripped), the menu treats it the way the hook does: the controls stay visible with defaults (speaking on, voice unset) rather than hiding until you fix it by hand.

Every edit the menu makes rewrites the value token in place, leaving every other line, including full-line `#` and `//` comments, untouched. The **Speaking** checkbox writes `enabled`. A voice selection writes `voice`, and writes `language` alongside it when that key is present, because a voice belongs to a language: leaving `language` at `a` after picking `jf_alpha` would have the hook synthesize Japanese through American English phonemes. The language half is best effort by design, so a config that has no `language` line still changes voice rather than refusing outright. If that key's value does not appear exactly once as a plain, uncommented scalar (an inline comment after the value, or the key duplicated, both break the match), the app refuses to write anything rather than guess. A refused write shows an error line in the menu and leaves the file exactly as it was: nothing is corrupted, and nothing silently fails to save either. The **Format** submenu writes `format` the same way, with one difference: since `format` is a newer key that most existing config files predate, a config with no `format` line yet has the key inserted just inside the opening brace instead of being refused outright. A config where `format` already appears more than once is still refused, the same ambiguity rule every other key follows.

## Running it at login

Not loaded by default. A LaunchAgent for it ships at `devices/macbook-pro-m5/local.kokoro-ttsmenu.plist`, set to restart on a crash but stay down after a deliberate Quit (`KeepAlive.SuccessfulExit: false`, the same convention as the daemon's own agent) and scoped to the graphical session (`LimitLoadToSessionType: Aqua`) since a menu bar icon has nowhere to go otherwise.

```bash
cp devices/macbook-pro-m5/local.kokoro-ttsmenu.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/local.kokoro-ttsmenu.plist
launchctl kickstart -p gui/$UID/local.kokoro-ttsmenu     # confirm it is up
```

Logs land in `~/Library/Logs/kokoro-ttsmenu.log`.

## What it will not do

It will not start the daemon just because you opened the menu. That is deliberate, not an oversight: the daemon costs about 1.2 GB resident (see [Cost](daemon.md#cost) in the daemon doc), and whether that trade is worth it for a given session is your call, not something a status icon should decide the moment it launches. Open the menu, see `not running`, and click **Restart daemon** when you actually want the model loaded.
