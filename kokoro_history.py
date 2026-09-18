"""Persistent store for the lines `kokoro-ttsd` has spoken.

The daemon used to synthesize into a temporary file, play it and unlink it, so
a line that was superseded, held back or simply missed was gone. This module
keeps the audio and an index of it, so "play that again" and "what did it say
while I was away" are answerable.

It is deliberately front-end-agnostic and torch-free, in the same spirit as
`paths.py`: the daemon owns it, `kokoro_menubar.py` reaches it through the
socket and never imports it. The store lives under the system temporary
directory, which makes it self-cleaning rather than archival - the OS may purge
it, and every reader here tolerates a directory or an index that vanished.

The index is append-only JSON Lines, oldest first, one short object per line.
A single `write` of a short line means the only corruption a crash can leave is
a truncated final line, which every reader skips.
"""

import datetime
import json
import os
import sys
import tempfile
from pathlib import Path

STORE_DIRNAME = "kokoro-ttsd"
INDEX_NAME = "index.jsonl"
HELD_NAME = "held"

# The formats the daemon accepts. `sweep` and `purge` use this to tell audio
# apart from the index and the hold marker, so nothing else may end in one.
AUDIO_SUFFIXES = (".wav", ".ogg", ".mp3")

HISTORY_MAX_ENTRIES = 200


def default_store_dir():
    """`tempfile.gettempdir()/kokoro-ttsd` - created on first use."""
    return os.path.join(tempfile.gettempdir(), STORE_DIRNAME)


def _status(message, is_error=False):
    print(f"{'error' if is_error else 'status'}: {message}", file=sys.stderr, flush=True)


def _utc_now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class HistoryStore:
    """The store directory, its index, its ids and its hold marker.

    `directory` is a constructor parameter rather than a module constant so a
    test can point one at `tmp_path` without monkeypatching
    `tempfile.gettempdir`, which every other consumer in the process also
    reads.
    """

    def __init__(self, directory=None, max_entries=HISTORY_MAX_ENTRIES):
        self.directory = str(directory) if directory is not None else default_store_dir()
        os.makedirs(self.directory, mode=0o700, exist_ok=True)
        self.max_entries = max_entries
        self._index_path = os.path.join(self.directory, INDEX_NAME)
        self._tmp_path = self._index_path + ".tmp"
        self._held_path = os.path.join(self.directory, HELD_NAME)
        # Continue the ids rather than restarting them, so a daemon restarted by
        # a LaunchAgent cannot hand out an id a client is still holding.
        self._next = max((int(entry["id"]) for entry in self._read_entries()), default=0) + 1

    # -- reading -----------------------------------------------------------

    def _read_entries(self, report=False):
        """Every parseable index entry, oldest first.

        `report` is set only at daemon start. During normal operation a
        corrupt line is skipped silently: `history` is polled, and one bad
        line would otherwise flood stderr on every call.
        """
        if not os.path.exists(self._index_path):
            return []
        entries = []
        try:
            handle = open(self._index_path, "r", encoding="utf-8", errors="replace")
        except OSError as error:
            if report:
                _status(f"history: cannot read {self._index_path}: {error}", True)
            return []
        with handle:
            for number, line in enumerate(handle, start=1):
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    entry = json.loads(stripped)
                except json.JSONDecodeError:
                    if report:
                        _status(f"history: skipping unparseable index line {number}.", True)
                    continue
                if not isinstance(entry, dict) or not entry.get("id") or not entry.get("format"):
                    if report:
                        _status(f"history: skipping index line {number}, missing id or format.", True)
                    continue
                try:
                    int(entry["id"])
                except (TypeError, ValueError):
                    if report:
                        _status(f"history: skipping index line {number}, id is not a number.", True)
                    continue
                entries.append(entry)
        return entries

    def entries(self, limit=15):
        """Newest first, at most `limit`. `None` means every entry."""
        newest_first = list(reversed(self._read_entries()))
        if limit is None:
            return newest_first
        return newest_first[: max(0, int(limit))]

    def find(self, entry_id):
        """The entry with this id, or None. Accepts `"0042"`, `"42"` and `42`."""
        try:
            wanted = int(entry_id)
        except (TypeError, ValueError):
            return None
        for entry in self._read_entries():
            if int(entry["id"]) == wanted:
                return entry
        return None

    def counts(self):
        """(entry count, total bytes of the audio files that still exist)."""
        entries = self._read_entries()
        total = 0
        for entry in entries:
            try:
                total += os.path.getsize(self.path_for(entry["id"], entry["format"]))
            except OSError:
                continue  # the file was purged or the OS swept tmp; the line stays
        return len(entries), total

    # -- ids and paths -----------------------------------------------------

    def next_id(self):
        """Reserve and return the next id, zero-padded to at least 4 digits.

        Called at accept time so the reply can carry an id before synthesis
        has run, which means a failed synthesis burns an id that never reaches
        the index. Gaps are legal; nothing here assumes contiguity.
        """
        entry_id = f"{self._next:04d}"
        self._next += 1
        return entry_id

    def path_for(self, entry_id, fmt):
        return os.path.join(self.directory, f"{entry_id}.{fmt}")

    # -- writing -----------------------------------------------------------

    def record(self, entry_id, text, voice, fmt, path):
        """Append one entry and prune to `max_entries`. Returns the entry."""
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        entry = {
            "id": str(entry_id),
            "text": text,
            "voice": voice,
            "format": fmt,
            "bytes": size,
            "created": _utc_now(),
        }
        with open(self._index_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")
        self._prune()
        return entry

    def _prune(self):
        """Drop the oldest entries past the cap. Returns how many went."""
        entries = self._read_entries()
        excess = len(entries) - self.max_entries
        if excess <= 0:
            return 0  # the common path: one append and no rewrite
        removed, surviving = entries[:excess], entries[excess:]
        # Rewrite through a sibling temporary and rename over the index, so a
        # crash mid-prune leaves either the pre-prune file or the new one.
        with open(self._tmp_path, "w", encoding="utf-8") as handle:
            for entry in surviving:
                handle.write(json.dumps(entry) + "\n")
        os.replace(self._tmp_path, self._index_path)
        for entry in removed:
            Path(self.path_for(entry["id"], entry["format"])).unlink(missing_ok=True)
        return len(removed)

    def sweep(self):
        """Delete audio files no parseable index line references.

        Called once at daemon start, before the first `next_id()`. It cleans up
        after a crash between writing the audio and appending the index line,
        and after a crash mid-prune, which leaves a stale `index.jsonl.tmp`.
        Returns the number of audio files removed.
        """
        Path(self._tmp_path).unlink(missing_ok=True)
        referenced = {str(entry["id"]) for entry in self._read_entries(report=True)}
        removed = 0
        for child in self._audio_files():
            if child.stem in referenced:
                continue
            child.unlink(missing_ok=True)
            removed += 1
        return removed

    def purge(self):
        """Delete every audio file and empty the index. Returns (removed, bytes).

        The id counter is not reset, so a client still holding an id from
        before the purge cannot be handed a different line under it. The hold
        marker is left alone: hold is a mode, not data.
        """
        removed = 0
        total = 0
        for child in self._audio_files():
            try:
                total += child.stat().st_size
            except OSError:
                pass
            child.unlink(missing_ok=True)
            removed += 1
        with open(self._index_path, "w", encoding="utf-8"):
            pass
        return removed, total

    def _audio_files(self):
        try:
            children = sorted(Path(self.directory).iterdir())
        except OSError:
            return []
        return [
            child
            for child in children
            if child.is_file() and child.suffix.lower() in AUDIO_SUFFIXES
        ]

    # -- hold --------------------------------------------------------------

    def held(self):
        """True when the hold marker is on disk.

        On disk rather than in memory so a daemon restart comes back still
        held, instead of burst-playing everything that arrives next.
        """
        return os.path.exists(self._held_path)

    def set_held(self, value):
        if value:
            Path(self._held_path).touch()
        else:
            Path(self._held_path).unlink(missing_ok=True)
        return bool(value)
