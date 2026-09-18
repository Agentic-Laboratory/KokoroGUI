"""Status bar front end for the warm synthesis daemon.

The daemon has no interface. Whether it is warm, which voice the Stop hook will
actually speak with, and whether speaking is switched on at all are answerable
only by reading `~/.claude/speak-bottom-line.json` and sending a ping down the
socket. This puts those three facts in the menu bar and makes them clickable.

It is a client of the daemon's socket and an editor of the hook's config file,
and nothing else. It never imports `kokoro_engine`, so it holds no pipeline and
costs nothing beyond a Python process; anything it cannot learn over the socket
it does not display.

Everything above `KokoroMenuBarApp` is deliberately free of rumps, which is
also the testing seam: rumps imports AppKit at module scope and CI runs on
Linux and Windows, so importing it at the top of this file would break
collection of the whole test module. It is imported inside `main()` and inside
the app class instead, and every decision worth asserting lives in a plain
function above them.
"""

import dataclasses
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import kokoro_cli
import kokoro_daemon
import paths

STATUS_WARM = "warm"
STATUS_STARTING = "starting"
STATUS_STOPPED = "not running"

# Published by the restart worker, never returned by `derive_status`, which
# only ever reports the three states above.
STATUS_RESTARTING = "restarting"

STATUS_GLYPHS = {STATUS_WARM: "●", STATUS_STARTING: "◐", STATUS_STOPPED: "○"}
UNKNOWN_VOICE = "—"
UNKNOWN_FORMAT = "—"

# Returned by speak_sample when the daemon accepted and recorded the sample
# but did not play it because output is held. Distinct from True (played) and
# False (failed outright), so a caller cannot mistake one for the other.
SPEAK_HELD = "held"

STARTING_GRACE = 30.0  # seconds a recent start keeps showing "starting"
PING_TIMEOUT = 1.5
POLL_INTERVAL = 2.0

# The same file the LaunchAgent plist writes to, so a manual start and a
# launchd start leave one log rather than two.
LOG_PATH = Path.home() / "Library" / "Logs" / "kokoro-ttsd.log"

CONFIG_ENV = "KOKORO_MENUBAR_CONFIG"
DEFAULT_CONFIG_PATH = Path.home() / ".claude" / "speak-bottom-line.json"


def config_path():
    """Resolve the hook's config file, honouring $KOKORO_MENUBAR_CONFIG.

    Read on every call rather than bound at import, so a test can redirect it
    after this module is already loaded. An empty value falls through.
    """
    override = os.environ.get(CONFIG_ENV)
    return Path(override) if override else DEFAULT_CONFIG_PATH


def strip_comment_lines(text):
    """Blank out full-line # and // comments, exactly as the hook's loader does.

    Lines are blanked rather than dropped so a JSON error still points at the
    line number the user sees in their editor.
    """
    return "\n".join(
        "" if line.lstrip().startswith(("#", "//")) else line
        for line in text.splitlines()
    )


def read_config():
    """Read the config: `None` when absent, `{}` when unusable, else the dict.

    The three states are distinct because they drive different menus. Absent
    hides the config-backed items entirely; unusable leaves them visible on the
    hook's own defaults, because a broken config is exactly what the hook falls
    back on too, and the menu must not disagree with what will be spoken.
    """
    path = config_path()
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return {}
    stripped = strip_comment_lines(raw)
    if not stripped.strip():
        return {}
    try:
        data = json.loads(stripped)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def config_enabled(config):
    """Whether the hook would speak. Mirrors its default of enabled: true."""
    if not isinstance(config, dict):
        return False
    return bool(config.get("enabled", True))


def display_voice(config, ping_reply):
    """The voice that will actually be used: the config outranks the daemon.

    The hook sends a voice with every request, so the daemon's own `serve`
    default only applies when the config names none.
    """
    for source in (config, ping_reply):
        if isinstance(source, dict):
            value = source.get("voice")
            if isinstance(value, str) and value.strip():
                return value
    return UNKNOWN_VOICE


def display_format(config, ping_reply):
    """The audio format that will actually be used: the config outranks the daemon."""
    for source in (config, ping_reply):
        if isinstance(source, dict):
            value = source.get("format")
            if isinstance(value, str) and value.strip():
                return value
    return UNKNOWN_FORMAT


# Two patterns, not one. The loose one finds every line that claims the key, so
# a line the strict one cannot parse - an inline comment after the value, say -
# is counted as a candidate and refuses the edit, instead of being skipped in
# favour of some other line that happens to parse.
_KEY_PATTERN = r'^\s*"{key}"\s*:'
_VALUE_PATTERN = (
    r'^(?P<head>\s*"{key}"\s*:\s*)'
    r'(?P<value>"(?:[^"\\]|\\.)*"|null|true|false|-?\d+(?:\.\d+)?)'
    r'(?P<tail>\s*,?\s*)$'
)


def resolved_socket(config=None):
    """The socket the hook would use, so both ends talk to the same daemon.

    The hook resolves its `socket` config key ahead of $KOKORO_TTS_SOCKET. A
    menu bar that ignored that key would ping the default path and report a
    confident status for a daemon that is not the one actually speaking. None
    means "no override", which is already what `kokoro_daemon.request` expects.
    """
    if config is None:
        config = read_config()
    if isinstance(config, dict):
        value = config.get("socket")
        if isinstance(value, str) and value.strip():
            return str(Path(value).expanduser())
    return None


def _config_key_lines(lines, key_pattern):
    """Every non-comment line whose stripped body matches key_pattern."""
    candidates = []
    for index, line in enumerate(lines):
        body = line.rstrip("\r\n")
        if body.lstrip().startswith(("#", "//")):
            continue
        if key_pattern.match(body):
            candidates.append((index, body, line[len(body):]))
    return candidates


def rewrite_config_token(text, key, value):
    """Replace one scalar in the config text, or return None to refuse.

    The file is edited as text rather than parsed and re-dumped because it is
    hand-annotated: `json.dump` would silently delete every comment in it,
    which is the worst thing this app could do to a file the user wrote. Only
    the value token is touched, so indentation, trailing commas and line
    endings all survive untouched.

    Refuses on anything other than exactly one non-comment line claiming the
    key, and on a value it cannot parse as a JSON scalar. Refusing leaves the
    file alone, which is always the safe direction.
    """
    if isinstance(value, (dict, list)):
        raise TypeError("rewrite_config_token writes JSON scalars only")

    quoted = re.escape(key)
    key_pattern = re.compile(_KEY_PATTERN.format(key=quoted))
    value_pattern = re.compile(_VALUE_PATTERN.format(key=quoted))

    lines = text.splitlines(keepends=True)
    candidates = _config_key_lines(lines, key_pattern)
    if len(candidates) != 1:
        return None

    index, body, ending = candidates[0]
    match = value_pattern.match(body)
    if match is None:
        return None
    lines[index] = match.group("head") + json.dumps(value) + match.group("tail") + ending
    return "".join(lines)


def insert_config_token(text, key, value):
    """Insert `"key": value` as a new line just after the opening brace.

    Refuses unless exactly one non-comment line's stripped body is a bare
    `{` - a config whose object opens on the same line as its first key
    (a hand-collapsed one-liner) is refused rather than guessed at, the same
    convention rewrite_config_token uses for an ambiguous match. Indentation
    is copied from the first existing key line after the brace, or defaults
    to two spaces when the object is otherwise empty. The new line reuses the
    brace line's own end-of-line sequence, so a CRLF file stays CRLF.
    """
    if isinstance(value, (dict, list)):
        raise TypeError("insert_config_token writes JSON scalars only")

    lines = text.splitlines(keepends=True)
    brace_lines = [
        index for index, line in enumerate(lines)
        if not line.rstrip("\r\n").lstrip().startswith(("#", "//"))
        and re.match(r'^\s*\{\s*$', line.rstrip("\r\n"))
    ]
    if len(brace_lines) != 1:
        return None

    brace_index = brace_lines[0]
    brace_line = lines[brace_index]
    ending = brace_line[len(brace_line.rstrip("\r\n")):] or "\n"

    indent = "  "
    has_following_key = False
    for line in lines[brace_index + 1:]:
        body = line.rstrip("\r\n")
        if body.lstrip().startswith(("#", "//")) or not body.strip():
            continue
        if body.strip() == "}":
            break
        stripped = body.lstrip(" \t")
        indent = body[: len(body) - len(stripped)]
        has_following_key = True
        break

    comma = "," if has_following_key else ""
    new_line = f'{indent}"{key}": {json.dumps(value)}{comma}{ending}'
    lines.insert(brace_index + 1, new_line)
    return "".join(lines)


def _rewrite_config_file(rewrite):
    """Read the config, apply `rewrite`, and replace the file atomically.

    `rewrite` returns the new text or None to refuse, and a refusal leaves the
    file untouched. Reading and writing with newline="" keeps CRLF endings from
    being rewritten behind the user's back, which universal-newline mode would
    do before `rewrite_config_token` ever saw them.
    """
    path = config_path()
    try:
        with open(path, "r", encoding="utf-8", newline="") as handle:
            text = handle.read()
    except (OSError, ValueError):
        return False

    updated = rewrite(text)
    if updated is None:
        return False

    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
        descriptor, temporary = tempfile.mkstemp(dir=str(path.parent))
    except OSError:
        return False
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            stream.write(updated)
        # mkstemp creates 0600; the config may be more permissive than that and
        # a replace has no business tightening it.
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        return True
    except OSError:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        return False


def write_config_token(key, value, insert_if_absent=False):
    """Rewrite one key in the config file. True only when the bytes landed.

    With insert_if_absent=True, a key on zero non-comment lines is inserted
    after the opening brace instead of refusing. A key on two or more lines
    still refuses either way: ambiguity is never resolved by guessing.
    """
    def rewrite(text):
        updated = rewrite_config_token(text, key, value)
        if updated is not None or not insert_if_absent:
            return updated
        lines = text.splitlines(keepends=True)
        key_pattern = re.compile(_KEY_PATTERN.format(key=re.escape(key)))
        if _config_key_lines(lines, key_pattern):
            return None  # 1+ non-comment lines already claim the key
        return insert_config_token(text, key, value)

    return _rewrite_config_file(rewrite)


def write_voice_and_language(voice, lang):
    """Set the voice, and the language alongside it where that key exists.

    A voice belongs to a language and the hook reads both keys: leaving
    `language` at `a` after picking `jf_alpha` would have every spoken line
    synthesized as Japanese through American English phonemes.

    The language half is deliberately best effort. A config with no `language`
    line is one the user trimmed, and refusing to change the voice at all over
    a key they chose not to keep would be the app breaking itself on their
    behalf. The voice write still refuses on its own terms.
    """
    def rewrite(text):
        updated = rewrite_config_token(text, "voice", voice)
        if updated is None:
            return None
        with_language = rewrite_config_token(updated, "language", lang)
        return updated if with_language is None else with_language

    return _rewrite_config_file(rewrite)


def ping(timeout=PING_TIMEOUT, socket_override=None):
    """Ask the daemon who it is. None when nothing usable answers.

    `OSError` covers a refused connection and a timeout, `ValueError` a reply
    that will not decode. A reply that decodes but reports failure comes back
    as a dict: judging it is `derive_status`'s job, not this one's.
    """
    try:
        return kokoro_daemon.request(
            {"command": "ping"},
            path=socket_override or resolved_socket(),
            timeout=timeout,
        )
    except (OSError, ValueError):
        return None


def stop_playback(timeout=PING_TIMEOUT, socket_override=None):
    """Cut off whatever is currently speaking."""
    try:
        reply = kokoro_daemon.request(
            {"command": "stop"},
            path=socket_override or resolved_socket(),
            timeout=timeout,
        )
    except (OSError, ValueError):
        return False
    return isinstance(reply, dict) and bool(reply.get("ok"))


def speak_sample(voice, lang, timeout=PING_TIMEOUT, socket_override=None):
    """Speak one line in `voice` so the user hears what they just selected.

    `lang` is always sent: `speak` falls back to the daemon's own language, so
    a jf_* voice would otherwise be synthesized as American English. No `wait`
    key, so the daemon acknowledges at once and synthesizes afterwards.
    `replace: true` so the sample is heard immediately rather than queuing
    behind whatever is already pending.

    Returns True on an ordinary accepted-and-played reply, False on any
    failure to reach or be accepted by the daemon, and the SPEAK_HELD sentinel
    when the daemon accepted and recorded the sample but did not play it
    because output is held - a case the caller must not mistake for either.
    """
    payload = {
        "command": "speak",
        "text": f"This is {voice}.",
        "voice": voice,
        "lang": lang,
        "replace": True,
    }
    try:
        reply = kokoro_daemon.request(
            payload, path=socket_override or resolved_socket(), timeout=timeout
        )
    except (OSError, ValueError):
        return False
    if not isinstance(reply, dict) or not reply.get("ok"):
        return False
    if reply.get("held"):
        return SPEAK_HELD
    return True


def replay(entry_id, timeout=PING_TIMEOUT, socket_override=None):
    """Ask the daemon to play back one history entry by id."""
    try:
        reply = kokoro_daemon.request(
            {"command": "replay", "id": entry_id},
            path=socket_override or resolved_socket(),
            timeout=timeout,
        )
    except (OSError, ValueError):
        return False
    return isinstance(reply, dict) and bool(reply.get("ok"))


def set_hold(held, timeout=PING_TIMEOUT, socket_override=None):
    """Engage or release hold. Returns the daemon's reported held state, or None on failure."""
    try:
        reply = kokoro_daemon.request(
            {"command": "hold" if held else "release"},
            path=socket_override or resolved_socket(),
            timeout=timeout,
        )
    except (OSError, ValueError):
        return None
    if isinstance(reply, dict) and reply.get("ok"):
        return bool(reply.get("held"))
    return None


def history(limit=15, timeout=PING_TIMEOUT, socket_override=None):
    """Ask the daemon for its most recent entries, newest first. None on failure."""
    try:
        return kokoro_daemon.request(
            {"command": "history", "limit": limit},
            path=socket_override or resolved_socket(),
            timeout=timeout,
        )
    except (OSError, ValueError):
        return None


def purge_history(timeout=PING_TIMEOUT, socket_override=None):
    """Ask the daemon to delete every stored history entry."""
    try:
        reply = kokoro_daemon.request(
            {"command": "purge"},
            path=socket_override or resolved_socket(),
            timeout=timeout,
        )
    except (OSError, ValueError):
        return None
    return reply if isinstance(reply, dict) and reply.get("ok") else None


def history_label(text, limit=60):
    """One line for a menu row: whitespace-collapsed, truncated with an ellipsis."""
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip() + "…"


def format_megabytes(num_bytes):
    """Decimal MB, one decimal place - matches Finder's units for the same tmp directory."""
    return f"{num_bytes / 1_000_000:.1f} MB"


def clear_history_label(count, num_bytes):
    unit = "file" if count == 1 else "files"
    return f"Clear history ({count} {unit}, {format_megabytes(num_bytes)})"


def shutdown_and_wait(timeout=10.0, interval=0.25, socket_override=None):
    """Ask the daemon to exit and wait until it has actually let go.

    Spawning before the socket is released hits `_clear_stale_socket`, which
    refuses to take over from a live listener and raises SystemExit rather than
    starting, so the wait is what makes a restart a restart.
    """
    target = socket_override or resolved_socket()
    try:
        kokoro_daemon.request(
            {"command": "shutdown"}, path=target, timeout=PING_TIMEOUT
        )
    except TimeoutError:
        # Delivered, but the daemon was too busy to answer in time. It is on
        # its way out, so fall through and poll: returning True here would let
        # the caller spawn over a socket the old daemon has not released yet,
        # and the new one would exit on `_clear_stale_socket`.
        pass
    except OSError:
        return True  # nothing was listening, so it is already down
    except ValueError:
        pass  # it answered with something unreadable; poll it out anyway

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ping(socket_override=target) is None or not os.path.exists(
            kokoro_daemon.socket_path(target)
        ):
            return True
        time.sleep(interval)
    return False


def spawn_daemon(log_path=None, socket_override=None):
    """Start a detached daemon, logging where the LaunchAgent logs.

    `start_new_session=True` is what lets the daemon outlive this app: quitting
    the menu bar must not take the warm pipeline down with it.

    A socket override has to be handed to the daemon too, not just used for our
    own requests: spawning on the default path while polling the override would
    leave a perfectly healthy daemon reading as "not running" forever. The flag
    goes after the subcommand, which is where `kokoro-ttsd` accepts it.
    """
    target = Path(log_path) if log_path is not None else LOG_PATH
    socket_target = socket_override or resolved_socket()
    command = [sys.executable, "-m", "kokoro_daemon", "serve"]
    if socket_target:
        command += ["--socket", socket_target]
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        stream = open(target, "a", encoding="utf-8")
    except OSError:
        # Losing the log is better than not starting.
        stream = None
    try:
        return subprocess.Popen(
            command,
            cwd=paths.APP_DIR,
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=stream if stream is not None else subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
        )
    finally:
        if stream is not None:
            stream.close()


def spawn_outcome(returncode, ping_reply):
    """Classify a spawn about a second in: started, already running, failed."""
    if returncode is None:
        return "started"
    if isinstance(ping_reply, dict) and ping_reply.get("ok"):
        # The child exited because `_clear_stale_socket` found a live listener.
        # Something is serving, which is what the user asked for: no error.
        return "already running"
    return "failed"


def derive_status(ping_reply, child_alive, started_at, now=None):
    """Reduce a ping, a child process and a start time to one status word."""
    if isinstance(ping_reply, dict) and bool(ping_reply.get("ok")):
        return STATUS_WARM
    # Ahead of the grace window on purpose. A first run downloads the model and
    # can legitimately take minutes, so a live child outranks any clock: a
    # process that is still up is still starting, however long it has been.
    if child_alive:
        return STATUS_STARTING
    moment = time.monotonic() if now is None else now
    if started_at is not None and moment - started_at < STARTING_GRACE:
        return STATUS_STARTING
    return STATUS_STOPPED


# One extra character, not a word: the bar title is the widest custom item in
# a bar already split by the notch and crowded on both sides, and macOS hides
# an item outright rather than shrink it, so ", held" going back in would
# reopen the exact problem the glyph-only title exists to fix. "‖" reads as
# a pause bar on sight, close enough to "held" without spelling it out.
HOLD_GLYPH = "‖"


def title_text(status, held=False):
    """The menu bar title itself: the status glyph, plus the hold glyph.

    Everything else `title_text` used to spell out - the status word and the
    voice - now lives in `status_line_text`, one click away in the dropdown
    instead of permanently occupying scarce bar width.
    """
    glyph = STATUS_GLYPHS.get(status, STATUS_GLYPHS[STATUS_STOPPED])
    return f"{glyph}{HOLD_GLYPH}" if held else glyph


def status_line_text(status, voice, held=False):
    """The full wording `title_text` used to render, now a disabled menu row."""
    suffix = ", held" if held else ""
    return f"Kokoro ({status}, {voice or UNKNOWN_VOICE}{suffix})"


def voice_menu_entries():
    """Language codes and their voices, in VOICE_DB order.

    The code rather than the display name, because the code is what a speak
    request's `lang` field and the config's `language` key both want. The lists
    are copies so a caller cannot reach back into VOICE_DB.
    """
    return [(code, list(voices)) for code, voices in kokoro_cli.VOICE_DB.items()]


@dataclasses.dataclass(frozen=True)
class Snapshot:
    """One consistent view of the world, frozen so a render cannot race it."""

    status: str
    voice: str
    enabled: bool
    has_config: bool
    pid: int | None
    message: str | None
    stamp: float
    fmt: str = UNKNOWN_FORMAT
    held: bool = False
    queued: int = 0
    history_count: int = 0
    history_bytes: int = 0
    history: tuple = ()


class KokoroMenuBarApp:
    """The status item, its menu, and the workers that keep both current.

    Composed around `rumps.App` rather than subclassing it: a subclass names
    `rumps` at class-definition time, which is import time, and the whole point
    of the split in this file is that importing it needs no AppKit.
    """

    def __init__(self):
        import rumps

        config = read_config()
        self._snapshot = Snapshot(
            status=STATUS_STOPPED,
            voice=display_voice(config, None),
            fmt=display_format(config, None),
            enabled=config_enabled(config),
            has_config=config is not None,
            pid=None,
            message=None,
            stamp=time.monotonic(),
        )
        self._rendered = None
        self._rendered_voice = None
        self._rendered_fmt = None
        self._poll_busy = False
        self._action_busy = False
        # Every snapshot rebind is taken under this lock. A bare rebind is
        # atomic, but `_publish` is a read-modify-write and the poll worker's
        # "is an action running" test has to be indivisible from its own write,
        # or the two clobber each other. `_action_generation` catches the case
        # the busy flag cannot: an action that starts *and finishes* while a
        # poll is parked in ping(), leaving the flag clear again by the time
        # the poll goes to publish data it sampled before the click.
        self._publish_lock = threading.Lock()
        self._action_generation = 0
        self._child = None
        self._started_at = None

        self.app = rumps.App(
            "Kokoro",
            title=title_text(self._snapshot.status, self._snapshot.held),
            quit_button="Quit",
        )

        # Held as attributes, never looked up by key: changing an item's title
        # later leaves its dict key stale, and assigning over an existing key
        # is a silent no-op, so the menu is mutated in place and never rebuilt.
        # No callback: this row is the label the bar title used to be, not an
        # action, so it renders disabled rather than clickable.
        self._status_line = rumps.MenuItem(
            status_line_text(self._snapshot.status, self._snapshot.voice, self._snapshot.held),
            callback=None,
        )
        self._speaking = rumps.MenuItem("Speaking", callback=self._on_toggle_speaking)
        self._voice = rumps.MenuItem("Voice")
        self._format = rumps.MenuItem("Format")
        self._hold = rumps.MenuItem("Hold output", callback=self._on_toggle_hold)
        self._stop = rumps.MenuItem("Stop speaking now", callback=self._on_stop)
        self._restart = rumps.MenuItem("Restart daemon", callback=self._on_restart)
        self._error = rumps.MenuItem("", callback=None)  # callback=None greys it out
        self._error.hidden = True

        self._voice_items = {}
        for code, voices in voice_menu_entries():
            language = kokoro_cli.LANGUAGES[code]
            language_item = rumps.MenuItem(language)
            for name in voices:
                item = rumps.MenuItem(name, callback=self._make_voice_callback(code, name))
                language_item[name] = item
                self._voice_items[name] = item
            self._voice[language] = language_item

        self._format_items = {}
        for value in ("wav", "ogg", "mp3"):
            item = rumps.MenuItem(value, callback=self._make_format_callback(value))
            self._format[value] = item
            self._format_items[value] = item

        # rumps' menu items cannot be rebuilt by key - assigning over an
        # existing key is a silent no-op - so the History submenu's fifteen
        # rows are fixed slots allocated once here and mutated in place on
        # every render, the same pattern self._error already uses.
        self._history = rumps.MenuItem("History")
        self._history_items = []
        for index in range(15):
            item = rumps.MenuItem("", callback=None)
            item.hidden = True
            self._history[str(index)] = item
            self._history_items.append(item)
        self._history_empty = rumps.MenuItem("(no history)", callback=None)
        self._history["(no history)"] = self._history_empty

        self._clear_history = rumps.MenuItem(
            "Clear history (0 files, 0.0 MB)", callback=self._on_clear_history
        )

        # rumps appends the Quit button at run time, so it lands below these.
        self.app.menu = [
            self._status_line,
            self._speaking,
            self._voice,
            self._format,
            None,
            self._hold,
            self._history,
            self._clear_history,
            self._stop,
            None,
            self._restart,
            self._error,
        ]
        self._timer = rumps.Timer(self._on_tick, POLL_INTERVAL)

    def run(self):
        """Start the poll timer and hand the main thread to the run loop."""
        self._timer.start()
        self.app.run()

    # Rendering. Main thread only: every one of these touches AppKit.

    def _render(self, snap):
        if snap is self._rendered:
            return
        self.app.title = title_text(snap.status, snap.held)
        self._status_line.title = status_line_text(snap.status, snap.voice, snap.held)
        self._speaking.state = 1 if snap.enabled else 0
        # Driven off the snapshot on every render, so creating or deleting the
        # config file takes effect within a tick instead of needing a relaunch.
        self._speaking.hidden = not snap.has_config
        self._voice.hidden = not snap.has_config
        self._format.hidden = not snap.has_config
        self._stop.set_callback(self._on_stop if snap.status == STATUS_WARM else None)
        self._restart.set_callback(
            None if snap.status == STATUS_RESTARTING else self._on_restart
        )
        if snap.voice != self._rendered_voice:  # 44 items; only worth it on a change
            for name, item in self._voice_items.items():
                item.state = 1 if name == snap.voice else 0
            self._rendered_voice = snap.voice
        if snap.fmt != self._rendered_fmt:  # 3 items; cheap regardless, guarded to match voice
            for value, item in self._format_items.items():
                item.state = 1 if value == snap.fmt else 0
            self._rendered_fmt = snap.fmt
        self._render_history(snap)
        self._hold.state = 1 if snap.held else 0
        self._hold.title = f"Hold output ({snap.queued} queued)" if snap.queued else "Hold output"
        self._hold.set_callback(self._on_toggle_hold if snap.status == STATUS_WARM else None)
        self._clear_history.title = clear_history_label(snap.history_count, snap.history_bytes)
        clear_active = snap.status == STATUS_WARM and snap.history_count > 0
        self._clear_history.set_callback(self._on_clear_history if clear_active else None)
        self._error.title = snap.message or ""
        self._error.hidden = snap.message is None
        self._rendered = snap

    def _render_history(self, snap):
        entries = snap.history
        if snap.status != STATUS_WARM:
            for item in self._history_items:
                item.hidden = True
            self._history_empty.title = "(daemon not running)"
            self._history_empty.hidden = False
            return
        if not entries:
            for item in self._history_items:
                item.hidden = True
            self._history_empty.title = "(no history)"
            self._history_empty.hidden = False
            return
        self._history_empty.hidden = True
        for index, item in enumerate(self._history_items):
            if index < len(entries):
                item.title = history_label(entries[index].get("text", ""))
                # The id, not the index: render is the one place a slot's
                # title and the entry it names are set together, on the main
                # thread, from this same `entries` list. Resolving it again
                # later against self._snapshot would not be "fresher" - a
                # poll can publish a reordered history before the next
                # render even runs, so by click time the live snapshot can
                # already disagree with the title still on screen.
                item.set_callback(self._make_history_callback(entries[index].get("id")))
                item.hidden = False
            else:
                item.hidden = True

    def _on_tick(self, _timer):
        self._render(self._snapshot)
        if self._poll_busy or self._action_busy:
            return  # an action owns the status line; do not fight it
        self._poll_busy = True
        threading.Thread(target=self._poll_worker, daemon=True).start()

    # Publishing. A snapshot is one attribute rebind, atomic under the GIL, so
    # a worker can hand the main thread a new view without a lock or a queue.

    def _publish(self, status=None, message=None, held=None):
        with self._publish_lock:
            base = self._snapshot
            self._snapshot = dataclasses.replace(
                base,
                status=base.status if status is None else status,
                held=base.held if held is None else held,
                message=message,
                stamp=time.monotonic(),
            )

    def _publish_config(self, message):
        """Publish the config as it is on disk, not as the click assumed.

        The checkmark follows the file, so a write that was refused never shows
        a change that did not land.
        """
        config = read_config()
        with self._publish_lock:
            base = self._snapshot
            self._snapshot = dataclasses.replace(
                base,
                # Keep whatever the last ping reported rather than flashing "—"
                # for a tick when the config itself names no voice or format.
                voice=display_voice(config, {"voice": base.voice}),
                fmt=display_format(config, {"format": base.fmt}),
                enabled=config_enabled(config),
                has_config=config is not None,
                message=message,
                stamp=time.monotonic(),
            )

    def _poll_worker(self):
        try:
            generation = self._action_generation
            previous = self._snapshot
            config = read_config()
            reply = ping()
            # Bound once: the restart worker sets self._child back to None
            # from its own thread, and reading the attribute twice can see it
            # alive on the first read and None on the second.
            child = self._child
            child_alive = child is not None and child.poll() is None
            status = derive_status(reply, child_alive, self._started_at)
            ok_reply = isinstance(reply, dict) and reply.get("ok")

            # An older daemon predating this feature sends none of these
            # fields, so every read goes through .get(..., default) and a
            # cold or unreachable daemon carries the previous tick forward
            # rather than flashing zeros.
            held = bool(reply.get("held", False)) if ok_reply else previous.held
            queued = reply.get("queued", 0) if ok_reply else previous.queued
            history_count = reply.get("history_count", 0) if ok_reply else previous.history_count
            history_bytes = reply.get("history_bytes", 0) if ok_reply else previous.history_bytes

            # Only refetched when the count moved: a tick where nothing
            # changed costs nothing beyond the ping already sent.
            history_entries = previous.history
            if ok_reply and history_count != previous.history_count:
                hist_reply = history(limit=15)
                if isinstance(hist_reply, dict) and hist_reply.get("ok"):
                    history_entries = tuple(hist_reply.get("entries", ()))

            snap = Snapshot(
                status=status,
                voice=display_voice(config, reply),
                fmt=display_format(config, reply),
                enabled=config_enabled(config),
                has_config=config is not None,
                pid=reply.get("pid") if status == STATUS_WARM else None,
                held=held,
                queued=queued,
                history_count=history_count,
                history_bytes=history_bytes,
                history=history_entries,
                # Carried forward rather than cleared: a poll two seconds later
                # would otherwise wipe an error before the user has opened the
                # menu to read it. Actions clear it when they start.
                message=previous.message,
                stamp=time.monotonic(),
            )
            # Re-checked here, not only at the top of the tick. A poll already
            # in flight when the user clicks Restart lands up to PING_TIMEOUT
            # later and would overwrite "restarting" with a status read before
            # the click - and since every later tick then skips polling, that
            # stale line would sit there for the whole restart.
            # The generation test is the half the busy flag misses: an action
            # that began and ended inside this poll leaves the flag clear, and
            # publishing here would quietly undo it - including wiping the
            # error line from a refused write, which is never re-published.
            with self._publish_lock:
                if self._action_busy or self._action_generation != generation:
                    return
                self._snapshot = snap
        finally:
            self._poll_busy = False

    # Actions. The click callback runs on the main thread and does nothing but
    # take the flag and hand the work to a thread; socket and file I/O in a
    # callback would freeze the menu for as long as it took.

    def _start_action(self, work):
        if self._action_busy:
            return  # the menu already shows what is happening; a second click is a no-op
        # Bumped under the lock so an in-flight poll cannot pass its staleness
        # test and then land its write on the far side of this click.
        with self._publish_lock:
            self._action_generation += 1
            self._action_busy = True
        threading.Thread(target=self._run_action, args=(work,), daemon=True).start()

    def _run_action(self, work):
        try:
            work()
        finally:
            self._action_busy = False

    def _on_stop(self, _sender):
        self._start_action(self._stop_worker)

    def _stop_worker(self):
        self._publish(message=None)
        if not stop_playback():
            self._publish(message="could not stop playback")

    def _on_toggle_hold(self, _sender):
        self._start_action(self._toggle_hold_worker)

    def _toggle_hold_worker(self):
        self._publish(message=None)
        result = set_hold(not self._snapshot.held)
        if result is None:
            self._publish(message="could not update hold state")
        else:
            # Reports the daemon's own held field, not the value the click
            # assumed, the same reasoning _publish_config already applies to
            # the config file: a write that was refused never shows a change
            # that did not land.
            self._publish(held=result, message=None)

    def _make_history_callback(self, entry_id):
        """Close over the id `_render_history` bound this slot to, not its
        index: the slot a click lands on is a screen position, but the entry
        at that position can already have shifted by the time the callback
        runs, and looking the position up again then would just re-read
        whatever the live snapshot has moved on to - not what the title on
        screen actually named."""
        def callback(_sender):
            self._start_action(lambda: self._replay_worker(entry_id))
        return callback

    def _replay_worker(self, entry_id):
        self._publish(message=None)
        if not any(entry.get("id") == entry_id for entry in self._snapshot.history):
            return  # the entry is gone by the time the click resolved; nothing to replay
        # stop first so the replayed line plays immediately instead of
        # queueing behind whatever is already pending: asyncio.Queue has no
        # front insertion.
        stop_playback()
        if not replay(entry_id):
            self._publish(message="could not replay that line")

    def _make_format_callback(self, value):
        def callback(_sender):
            self._start_action(lambda: self._format_worker(value))
        return callback

    def _format_worker(self, value):
        self._publish(message=None)
        if write_config_token("format", value, insert_if_absent=True):
            self._publish_config(None)
        else:
            self._publish_config(f"could not update {config_path()}")

    def _on_clear_history(self, _sender):
        self._start_action(self._clear_history_worker)

    def _clear_history_worker(self):
        self._publish(message=None)
        reply = purge_history()
        if reply is None:
            self._publish(message="could not clear history")
            return
        # Zeroed directly from the purge reply, rather than waiting for the
        # next poll, so the label updates the instant the click resolves.
        with self._publish_lock:
            base = self._snapshot
            self._snapshot = dataclasses.replace(
                base, history_count=0, history_bytes=0, history=(),
                message=None, stamp=time.monotonic(),
            )

    def _on_restart(self, _sender):
        self._start_action(self._restart_worker)

    def _restart_worker(self):
        # Also the Start button. With nothing listening, `shutdown_and_wait`
        # takes an OSError on the send and returns at once, so this falls
        # straight through to the spawn and no separate Start item is needed.
        self._publish(status=STATUS_RESTARTING, message=None)
        if not shutdown_and_wait():
            # Still answering, so still warm. Do not spawn over a live daemon.
            self._publish(status=STATUS_WARM, message="daemon did not stop")
            return
        try:
            child = spawn_daemon()
        except OSError as error:
            self._child = self._started_at = None
            self._publish(status=STATUS_STOPPED, message=f"could not start the daemon: {error}")
            return
        self._child = child
        self._started_at = time.monotonic()
        time.sleep(1.0)
        outcome = spawn_outcome(child.poll(), ping())
        if outcome == "failed":
            # Clearing `_started_at` skips the grace window, so the menu says
            # "not running" now instead of lying about it for another 30s.
            self._child = self._started_at = None
            self._publish(status=STATUS_STOPPED, message=f"daemon failed to start, see {LOG_PATH}")
            return
        self._publish(
            status=STATUS_STARTING if outcome == "started" else STATUS_WARM,
            message=None,
        )

    def _on_toggle_speaking(self, _sender):
        self._start_action(self._toggle_speaking_worker)

    def _toggle_speaking_worker(self):
        self._publish(message=None)
        ok = write_config_token("enabled", not self._snapshot.enabled)
        self._publish_config(None if ok else f"could not update {config_path()}")

    def _make_voice_callback(self, code, name):
        """Close over the language code too, so the sample can carry `lang`."""
        def callback(_sender):
            self._start_action(lambda: self._voice_worker(code, name))

        return callback

    def _voice_worker(self, code, name):
        # Config edits take the action flag like any other action: two rapid
        # clicks would otherwise race on the same file, the second reading it
        # between the first worker's read and its replace.
        self._publish(message=None)
        if not write_voice_and_language(name, code):
            self._publish_config(f"could not update {config_path()}")
            return
        # The write is what matters, so it is reported first and separately.
        # The sample is the audible confirmation, and a daemon that is not
        # running swallows it: say so rather than leave the user waiting for a
        # voice that is never coming. A held reply is neither: the daemon
        # accepted and recorded the sample but did not play it, which is
        # distinct from both success and failure and must not be reported as
        # either.
        result = speak_sample(name, code)
        if result is True:
            self._publish_config(None)
        elif result == SPEAK_HELD:
            self._publish_config(f"{name} saved, but output is held")
        else:
            self._publish_config(f"{name} saved, but the daemon is not speaking")


def main():
    """Run the status item. The run loop does not return until Quit."""
    # Imported here rather than at module scope: AppKit exists only on macOS,
    # and this module is imported on the Linux and Windows CI runners.
    from AppKit import NSApplication, NSApplicationActivationPolicyAccessory

    # rumps never sets an activation policy, so without this the app claims a
    # Dock icon and an application menu that a status item has no use for.
    NSApplication.sharedApplication().setActivationPolicy_(
        NSApplicationActivationPolicyAccessory
    )
    KokoroMenuBarApp().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
