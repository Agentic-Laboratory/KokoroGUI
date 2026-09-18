"""HistoryStore in isolation.

Every test points a store at `tmp_path`, which is why `directory` is a
constructor parameter: monkeypatching `tempfile.gettempdir` would move the
store for every other consumer in the process too.
"""
import json
import os
import time
from pathlib import Path

import pytest

import kokoro_history


@pytest.fixture
def store_dir(tmp_path):
    return tmp_path / "store"


@pytest.fixture
def store(store_dir):
    return kokoro_history.HistoryStore(store_dir)


def _seed(store, count, fmt="ogg", payload=b"audio"):
    """Record `count` entries with real placeholder audio behind each."""
    recorded = []
    for index in range(count):
        entry_id = store.next_id()
        path = store.path_for(entry_id, fmt)
        Path(path).write_bytes(payload)
        recorded.append(store.record(entry_id, f"line {index}", "af_sky", fmt, path))
    return recorded


def test_the_store_directory_is_created_on_first_use(store_dir):
    assert not store_dir.exists()
    store = kokoro_history.HistoryStore(store_dir)
    assert store_dir.is_dir()
    assert store.next_id() == "0001"


def test_default_store_dir_sits_under_the_system_temporary_directory():
    import tempfile

    assert kokoro_history.default_store_dir() == os.path.join(
        tempfile.gettempdir(), kokoro_history.STORE_DIRNAME
    )


def test_ids_are_zero_padded_and_monotonic(store):
    assert [store.next_id() for _ in range(3)] == ["0001", "0002", "0003"]


def test_a_fresh_store_continues_the_ids_of_an_existing_index(store, store_dir):
    _seed(store, 5)
    assert [entry["id"] for entry in store.entries()] == [
        "0005",
        "0004",
        "0003",
        "0002",
        "0001",
    ]
    restarted = kokoro_history.HistoryStore(store_dir)
    assert restarted.next_id() == "0006"


def test_a_gap_left_by_a_failed_synthesis_does_not_reset_the_counter(store, store_dir):
    """next_id() is called at accept time, so a failed render burns an id."""
    _seed(store, 2)
    store.next_id()  # reserved, synthesis failed, never recorded
    _seed(store, 1)
    assert [entry["id"] for entry in store.entries()] == ["0004", "0002", "0001"]
    restarted = kokoro_history.HistoryStore(store_dir)
    assert restarted.next_id() == "0005"


def test_path_for_names_the_file_after_the_id_and_the_format(store, store_dir):
    assert store.path_for("0042", "mp3") == str(store_dir / "0042.mp3")


def test_record_captures_the_size_the_text_and_a_utc_timestamp(store):
    entry_id = store.next_id()
    path = store.path_for(entry_id, "wav")
    Path(path).write_bytes(b"x" * 1234)
    entry = store.record(entry_id, "Exec summary: done.", "am_adam", "wav", path)
    assert entry["id"] == "0001"
    assert entry["text"] == "Exec summary: done."
    assert entry["voice"] == "am_adam"
    assert entry["format"] == "wav"
    assert entry["bytes"] == 1234
    assert entry["created"].endswith("Z") and len(entry["created"]) == 20


def test_entries_are_newest_first_and_honour_the_limit(store):
    _seed(store, 4)
    assert [entry["id"] for entry in store.entries(limit=2)] == ["0004", "0003"]
    assert len(store.entries(limit=None)) == 4


def test_an_empty_store_lists_nothing_rather_than_raising(store):
    assert store.entries() == []
    assert store.counts() == (0, 0)
    assert store.find("0001") is None


def test_find_accepts_a_padded_id_an_unpadded_one_and_an_integer(store):
    _seed(store, 3)
    assert store.find("0002")["text"] == "line 1"
    assert store.find("2")["text"] == "line 1"
    assert store.find(2)["text"] == "line 1"
    assert store.find("0099") is None
    assert store.find("not-an-id") is None


def test_inserting_past_the_cap_prunes_the_oldest_line_and_its_audio(store_dir):
    store = kokoro_history.HistoryStore(store_dir, max_entries=3)
    _seed(store, 3)
    oldest = Path(store.path_for("0001", "ogg"))
    assert oldest.exists()

    _seed(store, 1)  # the fourth entry pushes the first out

    assert [entry["id"] for entry in store.entries(limit=None)] == ["0004", "0003", "0002"]
    assert not oldest.exists()
    assert Path(store.path_for("0002", "ogg")).exists()
    assert Path(store.path_for("0004", "ogg")).exists()
    index_lines = (store_dir / kokoro_history.INDEX_NAME).read_text().splitlines()
    assert len(index_lines) == 3
    assert "0001" not in (store_dir / kokoro_history.INDEX_NAME).read_text()


def test_pruning_leaves_no_temporary_index_behind(store_dir):
    store = kokoro_history.HistoryStore(store_dir, max_entries=2)
    _seed(store, 5)
    assert not (store_dir / (kokoro_history.INDEX_NAME + ".tmp")).exists()
    assert len(store.entries(limit=None)) == 2


def test_a_store_at_the_cap_does_not_rewrite_on_every_append(store_dir):
    """The common path is one append. Pruning only fires past the cap."""
    store = kokoro_history.HistoryStore(store_dir, max_entries=200)
    _seed(store, 5)
    assert store._prune() == 0


def test_sweep_removes_orphaned_audio_and_a_stale_temporary_index(store, store_dir):
    _seed(store, 2)
    referenced = Path(store.path_for("0001", "ogg"))
    orphan = store_dir / "0099.ogg"
    orphan.write_bytes(b"crashed before the index line")
    stale_tmp = store_dir / (kokoro_history.INDEX_NAME + ".tmp")
    stale_tmp.write_text("half-written prune\n")
    store.set_held(True)

    assert store.sweep() == 1

    assert not orphan.exists()
    assert not stale_tmp.exists()
    assert referenced.exists()
    assert (store_dir / kokoro_history.INDEX_NAME).exists()
    assert (store_dir / kokoro_history.HELD_NAME).exists()
    assert len(store.entries()) == 2


def test_sweep_ignores_files_that_are_not_audio(store, store_dir):
    keep = store_dir / "notes.txt"
    keep.write_text("not ours")
    assert store.sweep() == 0
    assert keep.exists()


def test_a_corrupt_index_line_is_skipped_and_the_store_stays_usable(store, store_dir):
    _seed(store, 3)
    index = store_dir / kokoro_history.INDEX_NAME
    lines = index.read_text().splitlines()
    lines[1] = "not valid json"
    index.write_text("\n".join(lines) + "\n")

    assert [entry["id"] for entry in store.entries()] == ["0003", "0001"]
    assert store.find("0002") is None
    assert store.find("0003")["text"] == "line 2"
    assert store.counts()[0] == 2
    assert kokoro_history.HistoryStore(store_dir).next_id() == "0004"


def test_a_truncated_final_line_and_a_line_missing_keys_are_both_skipped(store, store_dir):
    _seed(store, 2)
    index = store_dir / kokoro_history.INDEX_NAME
    with index.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"id": "0003", "text": "no format key"}) + "\n")
        handle.write('{"id": "0004", "text": "truncated mid')  # no newline, no closing brace

    assert [entry["id"] for entry in store.entries()] == ["0002", "0001"]
    assert kokoro_history.HistoryStore(store_dir).next_id() == "0003"


def test_counts_sums_only_the_audio_that_still_exists(store):
    _seed(store, 3, payload=b"1234567890")
    assert store.counts() == (3, 30)
    Path(store.path_for("0002", "ogg")).unlink()
    assert store.counts() == (3, 20)


def test_purge_empties_the_directory_and_reports_what_it_removed(store, store_dir):
    _seed(store, 3, payload=b"0123456789")
    removed, total = store.purge()
    assert (removed, total) == (3, 30)
    assert store.entries() == []
    assert (store_dir / kokoro_history.INDEX_NAME).read_text() == ""
    assert not list(store_dir.glob("*.ogg"))


def test_purge_does_not_reset_the_id_counter(store):
    _seed(store, 2)
    store.purge()
    assert store.next_id() == "0003"


def test_purge_leaves_the_hold_marker_alone(store):
    store.set_held(True)
    _seed(store, 1)
    store.purge()
    assert store.held() is True


def test_the_hold_marker_survives_a_restart(store, store_dir):
    assert store.held() is False
    assert store.set_held(True) is True
    assert kokoro_history.HistoryStore(store_dir).held() is True
    assert store.set_held(False) is False
    assert kokoro_history.HistoryStore(store_dir).held() is False


def test_setting_hold_is_idempotent(store):
    store.set_held(True)
    store.set_held(True)
    assert store.held() is True
    store.set_held(False)
    store.set_held(False)
    assert store.held() is False


def test_two_records_a_second_apart_keep_their_insertion_order(store):
    """The index is ordered by position, not by the timestamp string."""
    _seed(store, 1)
    time.sleep(0.01)
    _seed(store, 1)
    assert [entry["id"] for entry in store.entries()] == ["0002", "0001"]
