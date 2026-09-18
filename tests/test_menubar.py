"""Menu bar front end tests.

Everything here exercises the rumps-free core of `kokoro_menubar.py`: config
reading and rewriting, status derivation, the socket wrappers, and spawning.
The suite's policy is the same as `test_daemon.py`'s: no model download, no
audio device, no real menu bar, no network. `kokoro_daemon.request` and
`subprocess.Popen` are stubbed throughout, and every test gets its own,
initially absent, config file so nothing here can ever touch the user's real
`~/.claude/speak-bottom-line.json`. Nothing imports rumps, and nothing in
this file needs to: `KokoroMenuBarApp` and `main()` are out of scope here,
per menubar-design.md section 5.
"""
import dataclasses
import json
import os
import stat
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import pytest

import kokoro_cli
import kokoro_menubar
import paths


@pytest.fixture(autouse=True)
def config_file(tmp_path, monkeypatch):
    """Point $KOKORO_MENUBAR_CONFIG at a tmp_path file, absent until written.

    Autouse so nothing in this module can read or write the real config, even
    indirectly through `resolved_socket()` or a socket wrapper's default path.
    """
    path = tmp_path / "speak-bottom-line.json"
    monkeypatch.setenv(kokoro_menubar.CONFIG_ENV, str(path))
    return path


class _StubRequest:
    """Records every call and lets a test script what `request` returns.

    Patched onto `kokoro_menubar.kokoro_daemon.request`, the module-object
    indirection `kokoro_menubar.py` uses specifically so this works.
    """

    def __init__(self):
        self.calls = []
        self.response = {"ok": True}
        self.error = None

    def __call__(self, payload, path=None, timeout=5.0, read_reply=True):
        self.calls.append({"payload": payload, "path": path, "timeout": timeout})
        if self.error is not None:
            raise self.error
        return self.response


@pytest.fixture
def stub_request(monkeypatch):
    stub = _StubRequest()
    monkeypatch.setattr(kokoro_menubar.kokoro_daemon, "request", stub)
    return stub


class _FakePopen:
    """Stands in for subprocess.Popen, recording the exact argv and kwargs."""

    last = None

    def __init__(self, command, **kwargs):
        self.command = command
        self.kwargs = kwargs
        self.returncode = None
        _FakePopen.last = self

    def poll(self):
        return self.returncode


@pytest.fixture
def fake_popen(monkeypatch):
    _FakePopen.last = None
    monkeypatch.setattr(kokoro_menubar.subprocess, "Popen", _FakePopen)
    return _FakePopen


# --- config_path -------------------------------------------------------------


def test_config_path_honours_the_environment_override(tmp_path, monkeypatch):
    override = tmp_path / "custom.json"
    monkeypatch.setenv(kokoro_menubar.CONFIG_ENV, str(override))
    assert kokoro_menubar.config_path() == override


def test_config_path_falls_back_to_the_default_when_unset(monkeypatch):
    monkeypatch.delenv(kokoro_menubar.CONFIG_ENV, raising=False)
    assert kokoro_menubar.config_path() == kokoro_menubar.DEFAULT_CONFIG_PATH


def test_config_path_falls_through_on_an_empty_override(monkeypatch):
    monkeypatch.setenv(kokoro_menubar.CONFIG_ENV, "")
    assert kokoro_menubar.config_path() == kokoro_menubar.DEFAULT_CONFIG_PATH


# --- strip_comment_lines -------------------------------------------------------


def test_strip_comment_lines_blanks_full_line_comments_but_keeps_the_line_count():
    text = '# leading comment\n{\n  // another comment\n  "enabled": true\n}\n'
    stripped = kokoro_menubar.strip_comment_lines(text)
    assert stripped.splitlines() == ["", "{", "", '  "enabled": true', "}"]
    assert len(stripped.splitlines()) == len(text.splitlines())


def test_strip_comment_lines_ignores_indentation_before_the_comment_marker():
    stripped = kokoro_menubar.strip_comment_lines('    # indented comment\n{}\n')
    assert stripped.splitlines()[0] == ""


# --- read_config: None / {} / dict tri-state ----------------------------------


def test_read_config_is_none_when_the_file_is_absent(config_file):
    assert kokoro_menubar.read_config() is None


def test_read_config_is_the_decoded_dict_when_valid(config_file):
    config_file.write_text('{"voice": "af_sky", "enabled": false}\n', encoding="utf-8")
    assert kokoro_menubar.read_config() == {"voice": "af_sky", "enabled": False}


def test_read_config_strips_full_line_comments_before_parsing(config_file):
    config_file.write_text(
        '// header comment\n{\n  # hash-style comment too\n  "voice": "af_sky"\n}\n',
        encoding="utf-8",
    )
    assert kokoro_menubar.read_config() == {"voice": "af_sky"}


def test_read_config_is_empty_dict_when_the_json_is_unparseable(config_file):
    config_file.write_text("{not valid json at all", encoding="utf-8")
    assert kokoro_menubar.read_config() == {}


def test_read_config_is_empty_dict_when_the_stripped_text_is_blank(config_file):
    config_file.write_text("// only a comment\n", encoding="utf-8")
    assert kokoro_menubar.read_config() == {}


def test_read_config_is_empty_dict_when_the_top_level_value_is_not_an_object(config_file):
    config_file.write_text("[1, 2, 3]\n", encoding="utf-8")
    assert kokoro_menubar.read_config() == {}


# --- config_enabled: matches the hook's own default ---------------------------


def test_config_enabled_true_by_default_on_a_broken_config():
    assert kokoro_menubar.config_enabled({}) is True


def test_config_enabled_false_when_the_config_is_absent():
    assert kokoro_menubar.config_enabled(None) is False


def test_config_enabled_honours_an_explicit_false():
    assert kokoro_menubar.config_enabled({"enabled": False}) is False


def test_config_enabled_honours_an_explicit_true():
    assert kokoro_menubar.config_enabled({"enabled": True}) is True


# --- display_voice: config outranks the ping reply ----------------------------


def test_display_voice_prefers_the_config_over_the_ping_reply():
    assert kokoro_menubar.display_voice({"voice": "af_bella"}, {"voice": "af_sky"}) == "af_bella"


def test_display_voice_falls_back_to_the_ping_reply_when_the_config_names_none():
    assert kokoro_menubar.display_voice({"voice": None}, {"voice": "af_sky"}) == "af_sky"
    assert kokoro_menubar.display_voice({}, {"voice": "af_sky"}) == "af_sky"
    assert kokoro_menubar.display_voice(None, {"voice": "af_sky"}) == "af_sky"


def test_display_voice_ignores_a_blank_string():
    assert kokoro_menubar.display_voice({"voice": "   "}, {"voice": "af_sky"}) == "af_sky"


def test_display_voice_placeholder_when_neither_source_names_one():
    assert kokoro_menubar.display_voice({}, {}) == kokoro_menubar.UNKNOWN_VOICE
    assert kokoro_menubar.display_voice(None, None) == kokoro_menubar.UNKNOWN_VOICE


# --- resolved_socket: the hook's socket key, expanded -------------------------


def test_resolved_socket_none_when_the_config_has_no_socket_key():
    assert kokoro_menubar.resolved_socket({"voice": "af_sky"}) is None


def test_resolved_socket_none_on_a_blank_or_non_string_value():
    assert kokoro_menubar.resolved_socket({"socket": ""}) is None
    assert kokoro_menubar.resolved_socket({"socket": None}) is None
    assert kokoro_menubar.resolved_socket({"socket": 5}) is None


def test_resolved_socket_expands_user_in_the_configured_path():
    assert kokoro_menubar.resolved_socket({"socket": "~/km.sock"}) == str(
        Path("~/km.sock").expanduser()
    )


def test_resolved_socket_reads_the_config_file_when_none_is_given(config_file):
    config_file.write_text('{"socket": "~/km-read.sock"}\n', encoding="utf-8")
    assert kokoro_menubar.resolved_socket() == str(Path("~/km-read.sock").expanduser())


# --- rewrite_config_token: the highest-stakes function in the module ----------

# Captured verbatim from ~/.claude/speak-bottom-line.json so the test runs
# identically wherever the suite executes, without ever opening that file.
REAL_CONFIG_SAMPLE = """// ~/.claude/speak-bottom-line.json
//
// Controls the Stop hook that speaks each reply's `Bottom line:` through
// Kokoro. Delete the file to turn speaking off entirely; every key below is
// optional and falls back to the value shown.
//
// Check what is actually in effect, and whether the daemon is answering:
//   python3 ~/.claude/hooks/speak-bottom-line.py --check
// Hear the current settings without waiting for a turn to end:
//   python3 ~/.claude/hooks/speak-bottom-line.py --test
//
// Full-line # and // comments are stripped before parsing. Inline comments
// after a value are not: keep them on their own line.
{
  // Master switch. false keeps the file and its settings but stays silent.
  "enabled": true,

  // Voice name from `kokoro-tts voices`. Omit to use the daemon's default
  // (af_heart). af_* are American female, am_* American male, bf_/bm_ British.
  "voice": "af_sky",

  // Speed multiplier. 1.0 is normal; 1.15 reads briskly without distorting.
  "speed": 1.0,

  // Kokoro language code or alias: a (en-us), b (en-gb), e, f, i, p, j, z.
  "language": "a",

  // What to speak:
  //   "bottom-line"       the Bottom line, else the opening paragraph
  //   "bottom-line-only"  the Bottom line, else nothing - quiet on short replies
  //   "first-paragraph"   always the opening paragraph
  "speak": "bottom-line",

  // Hard cap on the spoken text. A verdict should be a sentence or two.
  "max_chars": 400,

  // When the daemon is unreachable: "say" uses the macOS voice, so a dead
  // daemon is audible rather than silent; "none" stays quiet.
  "fallback": "say",
  "fallback_voice": "Samantha",

  // Daemon socket. Omit to use $KOKORO_TTS_SOCKET, then the default
  // ~/Code/KokoroGUI/daemon.sock.
  "socket": null
}
"""


def test_rewrite_config_token_against_the_real_config_keeps_every_comment_byte_identical():
    updated = kokoro_menubar.rewrite_config_token(REAL_CONFIG_SAMPLE, "voice", "af_heart")
    assert updated is not None
    original_lines = REAL_CONFIG_SAMPLE.splitlines()
    updated_lines = updated.splitlines()
    assert len(updated_lines) == len(original_lines)
    for original, new in zip(original_lines, updated_lines):
        if original.lstrip().startswith(("#", "//")):
            assert new == original
    assert '"voice": "af_heart",' in updated
    assert '"voice": "af_sky",' not in updated


def test_rewrite_config_token_matches_a_null_value():
    text = '{\n  "voice": null\n}\n'
    assert kokoro_menubar.rewrite_config_token(text, "voice", "af_bella") == (
        '{\n  "voice": "af_bella"\n}\n'
    )


def test_rewrite_config_token_refuses_on_zero_matching_lines():
    text = '{\n  "enabled": true\n}\n'
    assert kokoro_menubar.rewrite_config_token(text, "voice", "af_bella") is None


def test_rewrite_config_token_refuses_on_two_non_comment_matching_lines():
    text = '{\n  "voice": "af_sky",\n  "voice": "af_bella"\n}\n'
    assert kokoro_menubar.rewrite_config_token(text, "voice", "af_heart") is None


def test_rewrite_config_token_refuses_when_the_key_appears_only_on_a_comment_line():
    text = '{\n  # "voice": "af_sky"\n  "enabled": true\n}\n'
    assert kokoro_menubar.rewrite_config_token(text, "voice", "af_heart") is None


def test_rewrite_config_token_refuses_on_an_inline_comment_after_the_value():
    text = '{\n  "voice": "af_sky", // pick one\n}\n'
    assert kokoro_menubar.rewrite_config_token(text, "voice", "af_heart") is None


def test_rewrite_config_token_refuses_when_one_match_is_inline_commented_and_one_is_clean():
    """Candidacy is decided by the key alone, before either line's value is
    parsed, so a duplicate is refused even though only one of the two lines
    would ever have parsed as a value on its own."""
    text = '{\n  "voice": "af_sky", // pick one\n  "voice": "af_bella"\n}\n'
    assert kokoro_menubar.rewrite_config_token(text, "voice", "af_heart") is None


def test_rewrite_config_token_preserves_indentation_and_the_trailing_comma():
    text = '{\n    "voice": "af_sky",\n    "language": "a"\n}\n'
    updated = kokoro_menubar.rewrite_config_token(text, "voice", "af_bella")
    assert '    "voice": "af_bella",\n' in updated


def test_rewrite_config_token_keeps_a_missing_trailing_newline_missing():
    text = '{\n  "voice": "af_sky"\n}'
    updated = kokoro_menubar.rewrite_config_token(text, "voice", "af_bella")
    assert updated == '{\n  "voice": "af_bella"\n}'
    assert not updated.endswith("\n")


def test_rewrite_config_token_preserves_crlf_line_endings():
    text = '{\r\n  "voice": "af_sky",\r\n  "language": "a"\r\n}\r\n'
    updated = kokoro_menubar.rewrite_config_token(text, "voice", "af_bella")
    assert updated == '{\r\n  "voice": "af_bella",\r\n  "language": "a"\r\n}\r\n'


def test_rewrite_config_token_emits_lowercase_booleans():
    updated = kokoro_menubar.rewrite_config_token('{\n  "enabled": false\n}\n', "enabled", True)
    assert '"enabled": true' in updated
    assert "True" not in updated


def test_rewrite_config_token_raises_typeerror_for_a_dict_value():
    with pytest.raises(TypeError):
        kokoro_menubar.rewrite_config_token('{\n  "voice": "af_sky"\n}\n', "voice", {"a": 1})


def test_rewrite_config_token_raises_typeerror_for_a_list_value():
    with pytest.raises(TypeError):
        kokoro_menubar.rewrite_config_token('{\n  "voice": "af_sky"\n}\n', "voice", ["af_sky"])


def test_rewrite_config_token_refuses_exponent_form_numbers():
    text = '{\n  "speed": 1e5\n}\n'
    assert kokoro_menubar.rewrite_config_token(text, "speed", 1.2) is None


# --- _rewrite_config_file / write_config_token --------------------------------


def test_write_config_token_is_false_when_the_config_does_not_exist(config_file):
    assert kokoro_menubar.write_config_token("enabled", False) is False


def test_write_config_token_leaves_the_file_unchanged_after_a_refusal(config_file):
    original = '{\n  "voice": "af_sky",\n  "voice": "af_bella"\n}\n'  # duplicate key
    config_file.write_text(original, encoding="utf-8")
    assert kokoro_menubar.write_config_token("voice", "af_heart") is False
    assert config_file.read_text(encoding="utf-8") == original


def test_write_config_token_true_when_the_bytes_land(config_file):
    config_file.write_text('{\n  "voice": "af_sky"\n}\n', encoding="utf-8")
    assert kokoro_menubar.write_config_token("voice", "af_bella") is True
    assert config_file.read_text(encoding="utf-8") == '{\n  "voice": "af_bella"\n}\n'


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_write_config_token_preserves_the_file_permission_mode(config_file):
    config_file.write_text('{\n  "voice": "af_sky"\n}\n', encoding="utf-8")
    os.chmod(config_file, 0o640)
    assert kokoro_menubar.write_config_token("voice", "af_bella") is True
    assert stat.S_IMODE(os.stat(config_file).st_mode) == 0o640


# --- write_voice_and_language: voice always, language best effort -------------


def test_write_voice_and_language_updates_both_keys_when_present(config_file):
    config_file.write_text('{\n  "voice": "af_sky",\n  "language": "a"\n}\n', encoding="utf-8")
    assert kokoro_menubar.write_voice_and_language("jf_alpha", "j") is True
    updated = config_file.read_text(encoding="utf-8")
    assert '"voice": "jf_alpha"' in updated
    assert '"language": "j"' in updated


def test_write_voice_and_language_updates_the_voice_alone_when_language_is_absent(config_file):
    """A trimmed config with no `language` line is a deliberate user choice,
    not a broken file - the voice write must still land."""
    config_file.write_text('{\n  "voice": "af_sky"\n}\n', encoding="utf-8")
    assert kokoro_menubar.write_voice_and_language("jf_alpha", "j") is True
    assert config_file.read_text(encoding="utf-8") == '{\n  "voice": "jf_alpha"\n}\n'


def test_write_voice_and_language_refuses_when_the_voice_write_itself_refuses(config_file):
    original = '{\n  "voice": "af_sky",\n  "voice": "af_bella",\n  "language": "a"\n}\n'
    config_file.write_text(original, encoding="utf-8")
    assert kokoro_menubar.write_voice_and_language("jf_alpha", "j") is False
    assert config_file.read_text(encoding="utf-8") == original


# --- socket wrappers: ping, stop_playback, speak_sample -----------------------


def test_ping_returns_the_reply_dict_on_success(stub_request):
    stub_request.response = {"ok": True, "pid": 4242, "voice": "af_heart"}
    assert kokoro_menubar.ping() == {"ok": True, "pid": 4242, "voice": "af_heart"}
    assert stub_request.calls[0]["payload"] == {"command": "ping"}


def test_ping_returns_none_on_oserror(stub_request):
    stub_request.error = OSError("connection refused")
    assert kokoro_menubar.ping() is None


def test_ping_returns_none_on_a_reply_that_will_not_decode(stub_request):
    stub_request.error = json.JSONDecodeError("bad json", "doc", 0)
    assert kokoro_menubar.ping() is None


def test_ping_passes_a_socket_override_through_to_request(stub_request):
    kokoro_menubar.ping(socket_override="/tmp/km-override.sock")
    assert stub_request.calls[0]["path"] == "/tmp/km-override.sock"


def test_ping_falls_back_to_the_configured_socket_when_no_override_is_given(
    stub_request, config_file
):
    config_file.write_text('{"socket": "~/km-configured.sock"}\n', encoding="utf-8")
    kokoro_menubar.ping()
    assert stub_request.calls[0]["path"] == str(Path("~/km-configured.sock").expanduser())


def test_stop_playback_true_on_a_truthy_ok(stub_request):
    stub_request.response = {"ok": True}
    assert kokoro_menubar.stop_playback() is True


def test_stop_playback_false_on_a_falsy_ok(stub_request):
    stub_request.response = {"ok": False}
    assert kokoro_menubar.stop_playback() is False


def test_stop_playback_false_on_oserror(stub_request):
    stub_request.error = OSError()
    assert kokoro_menubar.stop_playback() is False


def test_stop_playback_passes_a_socket_override_through_to_request(stub_request):
    kokoro_menubar.stop_playback(socket_override="/tmp/km-stop.sock")
    assert stub_request.calls[0]["path"] == "/tmp/km-stop.sock"


def test_speak_sample_sends_lang_replace_and_omits_wait(stub_request):
    stub_request.response = {"ok": True}
    kokoro_menubar.speak_sample("jf_alpha", "j")
    payload = stub_request.calls[0]["payload"]
    assert payload == {
        "command": "speak",
        "text": "This is jf_alpha.",
        "voice": "jf_alpha",
        "lang": "j",
        "replace": True,
    }
    assert "wait" not in payload


def test_speak_sample_false_on_a_falsy_ok(stub_request):
    stub_request.response = {"ok": False}
    assert kokoro_menubar.speak_sample("af_sky", "a") is False


def test_speak_sample_passes_a_socket_override_through_to_request(stub_request):
    kokoro_menubar.speak_sample("af_sky", "a", socket_override="/tmp/km-speak.sock")
    assert stub_request.calls[0]["path"] == "/tmp/km-speak.sock"


def test_speak_sample_returns_the_held_sentinel_distinct_from_true_and_false(stub_request):
    stub_request.response = {"ok": True, "held": True}
    result = kokoro_menubar.speak_sample("af_sky", "a")
    assert result == kokoro_menubar.SPEAK_HELD
    assert result is not True
    assert result is not False


def test_speak_sample_true_when_ok_and_not_held(stub_request):
    stub_request.response = {"ok": True}
    assert kokoro_menubar.speak_sample("af_sky", "a") is True


# --- replay, set_hold, history, purge_history: the new socket wrappers --------


def test_replay_true_on_a_truthy_ok(stub_request):
    stub_request.response = {"ok": True}
    assert kokoro_menubar.replay("0007") is True
    assert stub_request.calls[0]["payload"] == {"command": "replay", "id": "0007"}


def test_replay_false_on_a_falsy_ok(stub_request):
    stub_request.response = {"ok": False}
    assert kokoro_menubar.replay("0007") is False


def test_replay_false_on_oserror(stub_request):
    stub_request.error = OSError()
    assert kokoro_menubar.replay("0007") is False


def test_replay_passes_a_socket_override_through_to_request(stub_request):
    kokoro_menubar.replay("0007", socket_override="/tmp/km-replay.sock")
    assert stub_request.calls[0]["path"] == "/tmp/km-replay.sock"


def test_set_hold_returns_the_daemons_reported_held_field(stub_request):
    stub_request.response = {"ok": True, "held": True}
    assert kokoro_menubar.set_hold(True) is True
    assert stub_request.calls[0]["payload"] == {"command": "hold"}


def test_set_hold_sends_release_for_false(stub_request):
    stub_request.response = {"ok": True, "held": False}
    assert kokoro_menubar.set_hold(False) is False
    assert stub_request.calls[0]["payload"] == {"command": "release"}


def test_set_hold_none_on_a_falsy_ok(stub_request):
    stub_request.response = {"ok": False}
    assert kokoro_menubar.set_hold(True) is None


def test_set_hold_none_on_oserror(stub_request):
    stub_request.error = OSError()
    assert kokoro_menubar.set_hold(True) is None


def test_set_hold_passes_a_socket_override_through_to_request(stub_request):
    stub_request.response = {"ok": True, "held": True}
    kokoro_menubar.set_hold(True, socket_override="/tmp/km-hold.sock")
    assert stub_request.calls[0]["path"] == "/tmp/km-hold.sock"


def test_history_returns_the_reply_dict_on_success(stub_request):
    stub_request.response = {"ok": True, "entries": [{"id": "1"}]}
    assert kokoro_menubar.history() == {"ok": True, "entries": [{"id": "1"}]}
    assert stub_request.calls[0]["payload"] == {"command": "history", "limit": 15}


def test_history_honours_a_custom_limit(stub_request):
    kokoro_menubar.history(limit=5)
    assert stub_request.calls[0]["payload"] == {"command": "history", "limit": 5}


def test_history_none_on_oserror(stub_request):
    stub_request.error = OSError()
    assert kokoro_menubar.history() is None


def test_purge_history_returns_the_reply_dict_on_success(stub_request):
    stub_request.response = {"ok": True, "removed": 3, "bytes": 900}
    assert kokoro_menubar.purge_history() == {"ok": True, "removed": 3, "bytes": 900}
    assert stub_request.calls[0]["payload"] == {"command": "purge"}


def test_purge_history_none_on_a_falsy_ok(stub_request):
    stub_request.response = {"ok": False}
    assert kokoro_menubar.purge_history() is None


def test_purge_history_none_on_oserror(stub_request):
    stub_request.error = OSError()
    assert kokoro_menubar.purge_history() is None


# --- display_format: config outranks the ping reply ---------------------------


def test_display_format_prefers_the_config_over_the_ping_reply():
    assert kokoro_menubar.display_format({"format": "wav"}, {"format": "ogg"}) == "wav"


def test_display_format_falls_back_to_the_ping_reply_when_the_config_names_none():
    assert kokoro_menubar.display_format({"format": None}, {"format": "ogg"}) == "ogg"
    assert kokoro_menubar.display_format({}, {"format": "ogg"}) == "ogg"
    assert kokoro_menubar.display_format(None, {"format": "ogg"}) == "ogg"


def test_display_format_placeholder_when_neither_source_names_one():
    assert kokoro_menubar.display_format({}, {}) == kokoro_menubar.UNKNOWN_FORMAT
    assert kokoro_menubar.display_format(None, None) == kokoro_menubar.UNKNOWN_FORMAT


# --- history_label: truncate for a menu row ------------------------------------


def test_history_label_collapses_internal_whitespace():
    assert kokoro_menubar.history_label("hello   \n  world") == "hello world"


def test_history_label_passes_text_at_or_under_the_limit_through_unchanged():
    text = "x" * 60
    assert kokoro_menubar.history_label(text) == text
    text_59 = "x" * 59
    assert kokoro_menubar.history_label(text_59) == text_59


def test_history_label_truncates_longer_text_with_an_ellipsis():
    text = "x" * 61
    result = kokoro_menubar.history_label(text)
    assert result == "x" * 59 + "…"
    assert len(result) == 60


# --- format_megabytes / clear_history_label ------------------------------------


def test_format_megabytes_renders_decimal_mb_with_one_decimal_place():
    assert kokoro_menubar.format_megabytes(0) == "0.0 MB"
    assert kokoro_menubar.format_megabytes(1_000_000) == "1.0 MB"


def test_clear_history_label_singular_and_plural_and_zero_state():
    assert kokoro_menubar.clear_history_label(0, 0) == "Clear history (0 files, 0.0 MB)"
    assert kokoro_menubar.clear_history_label(1, 500_000) == "Clear history (1 file, 0.5 MB)"
    label = kokoro_menubar.clear_history_label(12, 290100)
    assert label == f"Clear history (12 files, {kokoro_menubar.format_megabytes(290100)})"


# --- insert_config_token: the new-key insertion path ---------------------------


def test_insert_config_token_inserts_after_the_bare_opening_brace_with_matching_indentation():
    text = '{\n  "voice": "af_sky"\n}\n'
    updated = kokoro_menubar.insert_config_token(text, "format", "ogg")
    assert updated == '{\n  "format": "ogg",\n  "voice": "af_sky"\n}\n'


def test_insert_config_token_omits_the_trailing_comma_when_the_object_is_otherwise_empty():
    text = "{\n}\n"
    updated = kokoro_menubar.insert_config_token(text, "format", "ogg")
    assert updated == '{\n  "format": "ogg"\n}\n'


def test_insert_config_token_refuses_when_the_brace_shares_a_line_with_other_content():
    text = '{"voice": "af_sky"}\n'
    assert kokoro_menubar.insert_config_token(text, "format", "ogg") is None


def test_insert_config_token_refuses_when_the_opening_brace_line_appears_more_than_once():
    text = '{\n  "a":\n  {\n  }\n}\n'
    assert kokoro_menubar.insert_config_token(text, "format", "ogg") is None


def test_insert_config_token_preserves_crlf_endings():
    text = '{\r\n  "voice": "af_sky"\r\n}\r\n'
    updated = kokoro_menubar.insert_config_token(text, "format", "ogg")
    assert updated == '{\r\n  "format": "ogg",\r\n  "voice": "af_sky"\r\n}\r\n'


def test_insert_config_token_raises_typeerror_for_a_dict_value():
    with pytest.raises(TypeError):
        kokoro_menubar.insert_config_token("{\n}\n", "format", {"a": 1})


# --- write_config_token(insert_if_absent=True): the Format submenu's write ----


def test_write_config_token_inserts_a_new_key_when_absent_and_insert_if_absent_is_true(config_file):
    config_file.write_text('{\n  "voice": "af_sky"\n}\n', encoding="utf-8")
    assert kokoro_menubar.write_config_token("format", "ogg", insert_if_absent=True) is True
    assert config_file.read_text(encoding="utf-8") == '{\n  "format": "ogg",\n  "voice": "af_sky"\n}\n'


def test_write_config_token_still_refuses_two_conflicting_lines_with_insert_if_absent(config_file):
    original = '{\n  "format": "wav",\n  "format": "mp3"\n}\n'
    config_file.write_text(original, encoding="utf-8")
    assert kokoro_menubar.write_config_token("format", "ogg", insert_if_absent=True) is False
    assert config_file.read_text(encoding="utf-8") == original


def test_write_config_token_default_insert_if_absent_false_still_refuses_on_absent_key(config_file):
    config_file.write_text('{\n  "voice": "af_sky"\n}\n', encoding="utf-8")
    assert kokoro_menubar.write_config_token("format", "ogg") is False
    assert config_file.read_text(encoding="utf-8") == '{\n  "voice": "af_sky"\n}\n'


def test_write_config_token_format_writes_when_exactly_one_line_claims_it(config_file):
    """Mirrors the existing ambiguous/absent refusal already covered for
    voice/language, for the new `format` key specifically."""
    config_file.write_text('{\n  "format": "wav"\n}\n', encoding="utf-8")
    assert kokoro_menubar.write_config_token("format", "ogg") is True
    assert config_file.read_text(encoding="utf-8") == '{\n  "format": "ogg"\n}\n'


def test_write_config_token_format_refuses_cleanly_on_two_lines_or_none(config_file):
    two_lines = '{\n  "format": "wav",\n  "format": "mp3"\n}\n'
    config_file.write_text(two_lines, encoding="utf-8")
    assert kokoro_menubar.write_config_token("format", "ogg") is False
    assert config_file.read_text(encoding="utf-8") == two_lines

    no_line = '{\n  "voice": "af_sky"\n}\n'
    config_file.write_text(no_line, encoding="utf-8")
    assert kokoro_menubar.write_config_token("format", "ogg") is False
    assert config_file.read_text(encoding="utf-8") == no_line


# --- shutdown_and_wait ---------------------------------------------------------


def test_shutdown_and_wait_true_immediately_when_nothing_was_listening(stub_request):
    stub_request.error = OSError("connection refused")
    assert kokoro_menubar.shutdown_and_wait(timeout=0.3, interval=0.05) is True
    # The send itself raised, so the poll loop is never entered.
    assert len(stub_request.calls) == 1


def test_shutdown_and_wait_false_on_timeout_when_the_daemon_keeps_answering(
    stub_request, tmp_path
):
    still_there = tmp_path / "daemon.sock"
    still_there.touch()
    stub_request.response = {"ok": True}
    assert (
        kokoro_menubar.shutdown_and_wait(
            timeout=0.3, interval=0.05, socket_override=str(still_there)
        )
        is False
    )
    assert len(stub_request.calls) > 1  # the shutdown attempt, plus at least one poll


def test_shutdown_and_wait_passes_a_socket_override_through_to_request(stub_request):
    stub_request.error = OSError()
    kokoro_menubar.shutdown_and_wait(
        timeout=0.3, interval=0.05, socket_override="/tmp/km-shutdown.sock"
    )
    assert stub_request.calls[0]["path"] == "/tmp/km-shutdown.sock"


# --- spawn_daemon ----------------------------------------------------------------


def test_spawn_daemon_appends_the_socket_flag_after_serve_when_resolved(fake_popen, tmp_path):
    kokoro_menubar.spawn_daemon(log_path=tmp_path / "log.txt", socket_override="/tmp/km.sock")
    assert fake_popen.last.command == [
        sys.executable,
        "-m",
        "kokoro_daemon",
        "serve",
        "--socket",
        "/tmp/km.sock",
    ]


def test_spawn_daemon_omits_the_socket_flag_when_none_is_resolved(fake_popen, tmp_path):
    kokoro_menubar.spawn_daemon(log_path=tmp_path / "log.txt")
    assert fake_popen.last.command == [sys.executable, "-m", "kokoro_daemon", "serve"]


def test_spawn_daemon_runs_detached_from_the_app_directory(fake_popen, tmp_path):
    kokoro_menubar.spawn_daemon(log_path=tmp_path / "log.txt")
    kwargs = fake_popen.last.kwargs
    assert kwargs["cwd"] == paths.APP_DIR
    assert kwargs["start_new_session"] is True
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["stderr"] == subprocess.STDOUT


def test_spawn_daemon_falls_back_to_devnull_when_the_log_cannot_be_opened(fake_popen, tmp_path):
    # A file sits where the log's parent directory needs to go, so mkdir fails.
    blocker = tmp_path / "blocker"
    blocker.write_text("occupied", encoding="utf-8")
    kokoro_menubar.spawn_daemon(log_path=blocker / "kokoro-ttsd.log")
    assert fake_popen.last.kwargs["stdout"] == subprocess.DEVNULL


# --- spawn_outcome -----------------------------------------------------------------


def test_spawn_outcome_started_when_the_child_is_still_alive():
    assert kokoro_menubar.spawn_outcome(None, None) == "started"


def test_spawn_outcome_already_running_when_the_child_exited_but_ping_succeeds():
    assert kokoro_menubar.spawn_outcome(1, {"ok": True}) == "already running"


def test_spawn_outcome_failed_when_the_child_exited_and_no_ping_answers():
    assert kokoro_menubar.spawn_outcome(1, None) == "failed"


def test_spawn_outcome_failed_when_the_child_exited_and_ping_reports_not_ok():
    assert kokoro_menubar.spawn_outcome(1, {"ok": False}) == "failed"


# --- derive_status -----------------------------------------------------------------


def test_derive_status_warm_when_the_ping_reports_ok():
    assert kokoro_menubar.derive_status({"ok": True}, False, None) == kokoro_menubar.STATUS_WARM


def test_derive_status_a_falsy_ok_is_not_warm():
    """Documented non-obvious case: a reply that decodes but reports failure
    must not read as warm just because something answered."""
    status = kokoro_menubar.derive_status({"ok": False}, False, None, now=100.0)
    assert status == kokoro_menubar.STATUS_STOPPED


def test_derive_status_a_live_child_stays_starting_past_the_grace_window():
    """Documented non-obvious case: rule 2 (child_alive) precedes the grace
    window on purpose, because a first run downloads the model and can
    legitimately take minutes."""
    status = kokoro_menubar.derive_status(None, True, started_at=0.0, now=10_000.0)
    assert status == kokoro_menubar.STATUS_STARTING


def test_derive_status_starting_within_the_grace_window_with_no_live_child():
    status = kokoro_menubar.derive_status(None, False, started_at=100.0, now=110.0)
    assert status == kokoro_menubar.STATUS_STARTING


def test_derive_status_stopped_once_the_grace_window_has_elapsed():
    status = kokoro_menubar.derive_status(
        None, False, started_at=0.0, now=kokoro_menubar.STARTING_GRACE + 1
    )
    assert status == kokoro_menubar.STATUS_STOPPED


def test_derive_status_stopped_with_no_child_and_no_start_time():
    assert kokoro_menubar.derive_status(None, False, None, now=0.0) == kokoro_menubar.STATUS_STOPPED


# --- title_text -----------------------------------------------------------------


def test_title_text_is_the_bare_glyph_for_warm():
    assert kokoro_menubar.title_text("warm") == "●"


def test_title_text_falls_back_to_the_stopped_glyph_for_an_unrecognized_status():
    # "restarting" is published straight into a Snapshot by the restart
    # worker but never returned by derive_status, so it has no glyph of its
    # own; it falls back to the stopped glyph like any other unknown status.
    assert kokoro_menubar.title_text("restarting") == "○"


def test_title_text_held_appends_the_one_character_hold_glyph():
    assert kokoro_menubar.title_text("warm", held=True) == "●" + kokoro_menubar.HOLD_GLYPH


def test_title_text_held_defaults_to_false():
    assert kokoro_menubar.title_text("warm") == "●"
    assert kokoro_menubar.title_text("warm", held=False) == "●"


# --- status_line_text ------------------------------------------------------------


def test_status_line_text_exact_format_for_warm():
    assert kokoro_menubar.status_line_text("warm", "af_sky") == "Kokoro (warm, af_sky)"


def test_status_line_text_placeholder_for_an_unknown_voice():
    assert kokoro_menubar.status_line_text("not running", None) == "Kokoro (not running, —)"
    assert kokoro_menubar.status_line_text("not running", "") == "Kokoro (not running, —)"


def test_status_line_text_held_variant_appends_the_suffix():
    assert (
        kokoro_menubar.status_line_text("warm", "af_sky", held=True)
        == "Kokoro (warm, af_sky, held)"
    )


def test_status_line_text_held_defaults_to_false_and_matches_the_old_title_string():
    assert kokoro_menubar.status_line_text("warm", "af_sky") == "Kokoro (warm, af_sky)"


# --- voice_menu_entries -----------------------------------------------------------


def test_voice_menu_entries_covers_every_language_in_kokoro_cli():
    entries = kokoro_menubar.voice_menu_entries()
    codes = [code for code, _ in entries]
    assert len(codes) == 8
    assert set(codes) == set(kokoro_cli.LANGUAGES)


def test_voice_menu_entries_voice_names_are_globally_unique():
    entries = kokoro_menubar.voice_menu_entries()
    names = [name for _, voices in entries for name in voices]
    assert len(names) == len(set(names))


def test_voice_menu_entries_returns_copies_not_references_into_voice_db():
    entries = kokoro_menubar.voice_menu_entries()
    first_code, first_voices = entries[0]
    first_voices.append("bogus_voice")
    assert "bogus_voice" not in kokoro_cli.VOICE_DB[first_code]


# --- publish races ----------------------------------------------------------
#
# These build the app object with __new__ and set the attributes the workers
# touch, rather than constructing it: __init__ needs rumps, and this suite has
# to collect on the Linux and Windows CI runners where AppKit does not exist.


def _bare_app(**overrides):
    app = kokoro_menubar.KokoroMenuBarApp.__new__(kokoro_menubar.KokoroMenuBarApp)
    app._snapshot = kokoro_menubar.Snapshot(
        status="warm", voice="af_sky", enabled=True, has_config=True,
        pid=1, message=None, stamp=0.0,
    )
    app._poll_busy = False
    app._action_busy = False
    app._child = None
    app._started_at = None
    app._publish_lock = threading.Lock()
    app._action_generation = 0
    for name, value in overrides.items():
        setattr(app, name, value)
    return app


def test_a_poll_does_not_wipe_an_error_published_while_it_was_in_flight(monkeypatch):
    """The failure the `_action_busy` flag alone cannot catch.

    An action that starts *and finishes* while a poll is parked in `ping()`
    leaves the flag clear again by the time the poll publishes, so the poll
    would land data it sampled before the click. For a refused write that
    means the error line is lost outright: nothing ever re-publishes it, and
    the user sees a click that did nothing and said nothing.
    """
    app = _bare_app()
    # Events rather than sleeps: the ordering is the whole point of the test,
    # and a loaded CI runner must not be able to reorder it.
    inside_ping = threading.Event()
    release_ping = threading.Event()

    def blocking_ping(*args, **kwargs):
        inside_ping.set()
        release_ping.wait(timeout=5)
        return {"ok": True, "pid": 1, "voice": "af_heart"}

    monkeypatch.setattr(kokoro_menubar, "ping", blocking_ping)

    worker = threading.Thread(target=app._poll_worker, daemon=True)
    worker.start()
    assert inside_ping.wait(timeout=5), "the poll never reached ping()"

    # The action begins and ends entirely while the poll is parked, so
    # _action_busy is False again before the poll goes to publish.
    app._start_action(lambda: app._publish_config("could not update the config"))
    deadline = time.monotonic() + 5
    while app._action_busy and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not app._action_busy, "the action never finished"
    assert app._snapshot.message == "could not update the config"

    release_ping.set()
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert app._snapshot.message == "could not update the config"


def test_a_poll_still_publishes_when_no_action_intervened(monkeypatch):
    """The guard must not be so broad that ordinary polling stops working."""
    app = _bare_app()
    monkeypatch.setattr(
        kokoro_menubar, "ping", lambda *a, **k: {"ok": True, "pid": 77, "voice": "af_heart"}
    )
    app._poll_worker()
    assert app._snapshot.pid == 77
    assert app._snapshot.status == "warm"


# --- shutdown_and_wait timeout ----------------------------------------------


def test_a_timed_out_shutdown_is_polled_out_rather_than_assumed_gone(monkeypatch):
    """A timeout means delivered-but-slow, not absent.

    Returning True here would let the caller spawn over a socket the old
    daemon has not released, and the new one would exit on the daemon's own
    `_clear_stale_socket` refusal.
    """
    calls = []

    def fake_request(payload, path=None, timeout=5.0, read_reply=True):
        calls.append(payload["command"])
        raise TimeoutError("timed out")

    monkeypatch.setattr(kokoro_menubar.kokoro_daemon, "request", fake_request)
    monkeypatch.setattr(kokoro_menubar, "ping", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(kokoro_menubar.os.path, "exists", lambda path: True)

    assert kokoro_menubar.shutdown_and_wait(timeout=0.2, interval=0.05) is False
    assert calls == ["shutdown"]


def test_a_refused_shutdown_means_nothing_was_listening(monkeypatch):
    def fake_request(payload, path=None, timeout=5.0, read_reply=True):
        raise ConnectionRefusedError("nothing there")

    monkeypatch.setattr(kokoro_menubar.kokoro_daemon, "request", fake_request)
    assert kokoro_menubar.shutdown_and_wait(timeout=0.2, interval=0.05) is True


# --- voice selection reporting ----------------------------------------------


def test_a_voice_saved_while_the_daemon_is_down_says_so(monkeypatch):
    """A written voice with no audible sample must not look like success.

    The Voice submenu is not greyed when the daemon is cold, so this is an
    ordinary click, and the sample is the only feedback the user gets.
    """
    app = _bare_app()
    monkeypatch.setattr(kokoro_menubar, "write_voice_and_language", lambda voice, lang: True)
    monkeypatch.setattr(kokoro_menubar, "speak_sample", lambda voice, lang: False)

    app._voice_worker("j", "jf_alpha")
    assert "jf_alpha" in app._snapshot.message
    assert "not speaking" in app._snapshot.message


def test_a_voice_saved_with_a_working_daemon_reports_no_error(monkeypatch):
    app = _bare_app()
    monkeypatch.setattr(kokoro_menubar, "write_voice_and_language", lambda voice, lang: True)
    monkeypatch.setattr(kokoro_menubar, "speak_sample", lambda voice, lang: True)

    app._voice_worker("a", "af_sky")
    assert app._snapshot.message is None


def test_a_voice_saved_but_held_reports_that_distinctly(monkeypatch):
    """A held reply is neither success nor failure and must read as its own
    outcome, not as the daemon-is-not-speaking case True/False already cover."""
    app = _bare_app()
    monkeypatch.setattr(kokoro_menubar, "write_voice_and_language", lambda voice, lang: True)
    monkeypatch.setattr(
        kokoro_menubar, "speak_sample", lambda voice, lang: kokoro_menubar.SPEAK_HELD
    )

    app._voice_worker("j", "jf_alpha")
    assert "jf_alpha" in app._snapshot.message
    assert "held" in app._snapshot.message
    assert "not speaking" not in app._snapshot.message


# --- the app class, against a stubbed rumps ---------------------------------
#
# `kokoro_menubar` imports rumps inside the class body precisely so it can be
# substituted here. The assertions are all on real state - Snapshot fields,
# item flags the app itself set - so the stub is scaffolding, never the thing
# under test. This is what lets requirement 8's "stub the socket and rumps"
# hold on a runner with no AppKit.


class FakeMenuItem:
    def __init__(self, title, callback=None, key=None, **kwargs):
        self.title = title
        self.callback = callback
        self.state = 0
        self.hidden = False
        self.submenu = {}

    def set_callback(self, callback, key=None):
        self.callback = callback

    def __setitem__(self, key, value):
        # rumps silently ignores a key that already exists; mirror that, since
        # the app's "never rebuild, always mutate" rule depends on it.
        self.submenu.setdefault(key, value)

    def __getitem__(self, key):
        return self.submenu[key]


class FakeApp:
    def __init__(self, name, title=None, quit_button=None, **kwargs):
        self.name = name
        self.title = title
        self.quit_button = quit_button
        self.menu = []

    def run(self):
        raise AssertionError("the run loop must never start in a test")


class FakeTimer:
    def __init__(self, callback, interval):
        self.callback = callback
        self.interval = interval
        self.started = False

    def start(self):
        self.started = True


@pytest.fixture
def fake_rumps(monkeypatch):
    module = types.ModuleType("rumps")
    module.App = FakeApp
    module.MenuItem = FakeMenuItem
    module.Timer = FakeTimer
    monkeypatch.setitem(sys.modules, "rumps", module)
    return module


def test_the_menu_is_built_with_every_voice_nested_under_its_language(fake_rumps):
    app = kokoro_menubar.KokoroMenuBarApp()
    languages = app._voice.submenu
    assert len(languages) == 8
    total = sum(len(item.submenu) for item in languages.values())
    assert total == len(app._voice_items) == 44
    assert len(app._format_items) == 3
    assert len(app._history_items) == 15
    # Two separators: after Format, and before Restart.
    assert app.app.menu.count(None) == 2
    assert app.app.quit_button == "Quit"


def test_speaking_and_voice_are_hidden_when_there_is_no_config(fake_rumps):
    app = kokoro_menubar.KokoroMenuBarApp()
    app._render(dataclasses.replace(app._snapshot, has_config=False, stamp=1.0))
    assert app._speaking.hidden is True
    assert app._voice.hidden is True

    app._render(dataclasses.replace(app._snapshot, has_config=True, stamp=2.0))
    assert app._speaking.hidden is False
    assert app._voice.hidden is False


def test_the_speaking_checkmark_follows_the_enabled_flag(fake_rumps):
    app = kokoro_menubar.KokoroMenuBarApp()
    app._render(dataclasses.replace(app._snapshot, enabled=True, has_config=True, stamp=1.0))
    assert app._speaking.state == 1
    app._render(dataclasses.replace(app._snapshot, enabled=False, has_config=True, stamp=2.0))
    assert app._speaking.state == 0


def test_only_the_current_voice_is_checked(fake_rumps):
    app = kokoro_menubar.KokoroMenuBarApp()
    app._render(dataclasses.replace(app._snapshot, voice="af_sky", stamp=1.0))
    checked = [name for name, item in app._voice_items.items() if item.state == 1]
    assert checked == ["af_sky"]

    app._render(dataclasses.replace(app._snapshot, voice="jf_alpha", stamp=2.0))
    checked = [name for name, item in app._voice_items.items() if item.state == 1]
    assert checked == ["jf_alpha"]


def test_stop_is_greyed_out_unless_the_daemon_is_warm(fake_rumps):
    app = kokoro_menubar.KokoroMenuBarApp()
    app._render(dataclasses.replace(app._snapshot, status="not running", stamp=1.0))
    assert app._stop.callback is None
    app._render(dataclasses.replace(app._snapshot, status="warm", stamp=2.0))
    assert app._stop.callback is not None


def test_restart_is_greyed_out_while_a_restart_is_in_flight(fake_rumps):
    app = kokoro_menubar.KokoroMenuBarApp()
    app._render(dataclasses.replace(app._snapshot, status="restarting", stamp=1.0))
    assert app._restart.callback is None
    app._render(dataclasses.replace(app._snapshot, status="warm", stamp=2.0))
    assert app._restart.callback is not None


def test_the_error_row_appears_only_when_a_snapshot_carries_a_message(fake_rumps):
    app = kokoro_menubar.KokoroMenuBarApp()
    app._render(dataclasses.replace(app._snapshot, message=None, stamp=1.0))
    assert app._error.hidden is True

    app._render(dataclasses.replace(app._snapshot, message="daemon did not stop", stamp=2.0))
    assert app._error.hidden is False
    assert app._error.title == "daemon did not stop"


def test_the_bar_title_is_the_bare_glyph_and_the_menu_carries_the_detail(fake_rumps):
    app = kokoro_menubar.KokoroMenuBarApp()
    app._render(dataclasses.replace(app._snapshot, status="warm", voice="af_sky", stamp=1.0))
    assert app.app.title == "●"
    assert app._status_line.title == "Kokoro (warm, af_sky)"


def test_the_status_line_item_has_no_callback_so_it_renders_disabled(fake_rumps):
    app = kokoro_menubar.KokoroMenuBarApp()
    app._render(dataclasses.replace(app._snapshot, status="warm", voice="af_sky", stamp=1.0))
    assert app._status_line.callback is None


def test_the_status_line_is_the_first_item_in_the_menu(fake_rumps):
    app = kokoro_menubar.KokoroMenuBarApp()
    assert app.app.menu[0] is app._status_line
    assert app.app.menu[1] is app._speaking


def test_the_bar_title_appends_the_hold_glyph_when_held(fake_rumps):
    app = kokoro_menubar.KokoroMenuBarApp()
    app._render(dataclasses.replace(app._snapshot, status="warm", voice="af_sky", held=True, stamp=1.0))
    assert app.app.title == "●" + kokoro_menubar.HOLD_GLYPH
    assert app._status_line.title == "Kokoro (warm, af_sky, held)"


def test_a_tick_never_starts_a_poll_while_an_action_is_running(fake_rumps, monkeypatch):
    """The tick must not fight a restart for the status line."""
    app = kokoro_menubar.KokoroMenuBarApp()
    started = []
    monkeypatch.setattr(
        kokoro_menubar.threading, "Thread",
        lambda *a, **k: started.append(k.get("target")) or _NullThread(),
    )
    app._action_busy = True
    app._on_tick(None)
    assert started == []

    app._action_busy = False
    app._on_tick(None)
    assert started == [app._poll_worker]


class _NullThread:
    def start(self):
        pass


# --- Format submenu -----------------------------------------------------------


def test_format_submenu_hidden_unless_there_is_a_config(fake_rumps):
    app = kokoro_menubar.KokoroMenuBarApp()
    app._render(dataclasses.replace(app._snapshot, has_config=False, stamp=1.0))
    assert app._format.hidden is True
    app._render(dataclasses.replace(app._snapshot, has_config=True, stamp=2.0))
    assert app._format.hidden is False


def test_only_the_current_format_is_checked(fake_rumps):
    app = kokoro_menubar.KokoroMenuBarApp()
    app._render(dataclasses.replace(app._snapshot, fmt="ogg", stamp=1.0))
    checked = [value for value, item in app._format_items.items() if item.state == 1]
    assert checked == ["ogg"]

    app._render(dataclasses.replace(app._snapshot, fmt="wav", stamp=2.0))
    checked = [value for value, item in app._format_items.items() if item.state == 1]
    assert checked == ["wav"]


# --- History submenu ------------------------------------------------------------


def test_render_history_shows_daemon_not_running_placeholder_when_cold(fake_rumps):
    app = kokoro_menubar.KokoroMenuBarApp()
    app._render(dataclasses.replace(app._snapshot, status="not running", history=(), stamp=1.0))
    assert app._history_empty.hidden is False
    assert app._history_empty.title == "(daemon not running)"
    assert all(item.hidden for item in app._history_items)


def test_render_history_shows_no_history_placeholder_when_warm_and_empty(fake_rumps):
    app = kokoro_menubar.KokoroMenuBarApp()
    app._render(dataclasses.replace(app._snapshot, status="warm", history=(), stamp=1.0))
    assert app._history_empty.hidden is False
    assert app._history_empty.title == "(no history)"
    assert all(item.hidden for item in app._history_items)


def test_render_history_populates_slots_newest_first_and_hides_the_remainder(fake_rumps):
    app = kokoro_menubar.KokoroMenuBarApp()
    entries = tuple({"id": str(i), "text": f"line {i}"} for i in range(3))
    app._render(dataclasses.replace(app._snapshot, status="warm", history=entries, stamp=1.0))
    assert app._history_empty.hidden is True
    for index, item in enumerate(app._history_items):
        if index < 3:
            assert item.hidden is False
            assert item.title == f"line {index}"
        else:
            assert item.hidden is True


def test_replay_worker_sends_stop_then_replay_with_the_bound_entry_id(
    fake_rumps, monkeypatch
):
    """Per the resolved ruling: stop first, so a replayed line plays
    immediately instead of queueing behind whatever is already pending."""
    app = kokoro_menubar.KokoroMenuBarApp()
    app._snapshot = dataclasses.replace(
        app._snapshot, status="warm", history=({"id": "abc", "text": "hello"},), stamp=1.0
    )
    calls = []
    monkeypatch.setattr(kokoro_menubar, "stop_playback", lambda **k: calls.append("stop") or True)
    monkeypatch.setattr(
        kokoro_menubar, "replay", lambda entry_id, **k: calls.append(("replay", entry_id)) or True
    )

    app._replay_worker("abc")

    assert calls == ["stop", ("replay", "abc")]


def test_replay_worker_is_a_noop_when_the_bound_id_is_no_longer_in_history(
    fake_rumps, monkeypatch
):
    """Covers a purge or a trim off the end of the list landing between the
    render that bound the id and the click resolving it: the id the user
    clicked is simply gone, so there is nothing to interrupt or replay."""
    app = kokoro_menubar.KokoroMenuBarApp()
    app._snapshot = dataclasses.replace(app._snapshot, status="warm", history=(), stamp=1.0)
    calls = []
    monkeypatch.setattr(kokoro_menubar, "stop_playback", lambda **k: calls.append("stop") or True)
    monkeypatch.setattr(
        kokoro_menubar, "replay", lambda entry_id, **k: calls.append(("replay", entry_id)) or True
    )

    app._replay_worker("abc")

    assert calls == []


def test_replay_worker_reports_an_error_when_replay_fails(fake_rumps, monkeypatch):
    app = kokoro_menubar.KokoroMenuBarApp()
    app._snapshot = dataclasses.replace(
        app._snapshot, status="warm", history=({"id": "abc", "text": "hello"},), stamp=1.0
    )
    monkeypatch.setattr(kokoro_menubar, "stop_playback", lambda **k: True)
    monkeypatch.setattr(kokoro_menubar, "replay", lambda entry_id, **k: False)

    app._replay_worker("abc")

    assert app._snapshot.message == "could not replay that line"


def test_a_click_replays_the_id_that_was_on_screen_even_if_history_reordered_after_render(
    fake_rumps, monkeypatch
):
    """The real risk in the fixed-slot pattern: a poll can publish a
    reordered history after a render but before the next one - a dropdown
    open and tracking the mouse does not tick - so by the time a click
    resolves, self._snapshot can already disagree with what is on screen.
    Binding the id at render time, not re-resolving it from the live
    snapshot at click time, is what keeps the click and the title in sync.
    """
    app = kokoro_menubar.KokoroMenuBarApp()
    rendered = (
        {"id": "a", "text": "line A"},
        {"id": "b", "text": "line B"},
    )
    app._render(dataclasses.replace(app._snapshot, status="warm", history=rendered, stamp=1.0))
    clicked = app._history_items[1].callback  # bound while "line B" was on screen
    assert app._history_items[1].title == "line B"

    # A poll lands after the render, prepending a new line, with no
    # re-render in between to catch the shift up.
    reordered = (
        {"id": "c", "text": "line C"},
        {"id": "a", "text": "line A"},
        {"id": "b", "text": "line B"},
    )
    app._snapshot = dataclasses.replace(app._snapshot, history=reordered, stamp=2.0)

    calls = []
    monkeypatch.setattr(kokoro_menubar, "stop_playback", lambda **k: calls.append("stop") or True)
    monkeypatch.setattr(
        kokoro_menubar, "replay", lambda entry_id, **k: calls.append(("replay", entry_id)) or True
    )

    clicked(None)
    deadline = time.monotonic() + 5
    while app._action_busy and time.monotonic() < deadline:
        time.sleep(0.01)

    assert calls == ["stop", ("replay", "b")]


# --- Hold output ----------------------------------------------------------------


def test_hold_checkbox_state_and_title_follow_the_snapshot(fake_rumps):
    app = kokoro_menubar.KokoroMenuBarApp()
    app._render(dataclasses.replace(app._snapshot, status="warm", held=False, queued=0, stamp=1.0))
    assert app._hold.state == 0
    assert app._hold.title == "Hold output"
    assert app._hold.callback is not None

    app._render(dataclasses.replace(app._snapshot, status="warm", held=True, queued=3, stamp=2.0))
    assert app._hold.state == 1
    assert app._hold.title == "Hold output (3 queued)"


def test_hold_callback_is_greyed_out_unless_the_daemon_is_warm(fake_rumps):
    app = kokoro_menubar.KokoroMenuBarApp()
    app._render(dataclasses.replace(app._snapshot, status="not running", stamp=1.0))
    assert app._hold.callback is None
    app._render(dataclasses.replace(app._snapshot, status="warm", stamp=2.0))
    assert app._hold.callback is not None


def test_toggle_hold_worker_publishes_the_daemons_reported_state_not_the_assumed_one(
    fake_rumps, monkeypatch
):
    app = kokoro_menubar.KokoroMenuBarApp()
    app._snapshot = dataclasses.replace(app._snapshot, held=False, stamp=1.0)
    # The click assumes True (not held -> held), but the daemon reports False
    # (a LaunchAgent restart re-read a persisted release, say); the checkbox
    # must follow the daemon, not the click.
    monkeypatch.setattr(kokoro_menubar, "set_hold", lambda held, **k: False)

    app._toggle_hold_worker()

    assert app._snapshot.held is False
    assert app._snapshot.message is None


def test_toggle_hold_worker_publishes_an_error_when_set_hold_fails(fake_rumps, monkeypatch):
    app = kokoro_menubar.KokoroMenuBarApp()
    app._snapshot = dataclasses.replace(app._snapshot, held=False, stamp=1.0)
    monkeypatch.setattr(kokoro_menubar, "set_hold", lambda held, **k: None)

    app._toggle_hold_worker()

    assert app._snapshot.message == "could not update hold state"


# --- Clear history ---------------------------------------------------------------


def test_clear_history_callback_gated_on_history_count_and_warm_status(fake_rumps):
    app = kokoro_menubar.KokoroMenuBarApp()
    app._render(dataclasses.replace(app._snapshot, status="warm", history_count=0, stamp=1.0))
    assert app._clear_history.callback is None
    app._render(dataclasses.replace(app._snapshot, status="not running", history_count=5, stamp=2.0))
    assert app._clear_history.callback is None
    app._render(dataclasses.replace(app._snapshot, status="warm", history_count=5, stamp=3.0))
    assert app._clear_history.callback is not None


def test_clear_history_label_reflects_the_snapshot(fake_rumps):
    app = kokoro_menubar.KokoroMenuBarApp()
    app._render(dataclasses.replace(app._snapshot, history_count=12, history_bytes=290100, stamp=1.0))
    assert app._clear_history.title == kokoro_menubar.clear_history_label(12, 290100)


def test_clear_history_worker_zeroes_counts_without_waiting_for_a_poll(fake_rumps, monkeypatch):
    app = kokoro_menubar.KokoroMenuBarApp()
    app._snapshot = dataclasses.replace(
        app._snapshot, history_count=5, history_bytes=12345, history=({"id": "1"},), stamp=1.0
    )
    monkeypatch.setattr(kokoro_menubar, "purge_history", lambda **k: {"ok": True, "removed": 5})

    app._clear_history_worker()

    assert app._snapshot.history_count == 0
    assert app._snapshot.history_bytes == 0
    assert app._snapshot.history == ()
    assert app._snapshot.message is None


def test_clear_history_worker_reports_an_error_on_failure(fake_rumps, monkeypatch):
    app = kokoro_menubar.KokoroMenuBarApp()
    app._snapshot = dataclasses.replace(app._snapshot, history_count=5, stamp=1.0)
    monkeypatch.setattr(kokoro_menubar, "purge_history", lambda **k: None)

    app._clear_history_worker()

    assert app._snapshot.message == "could not clear history"
    assert app._snapshot.history_count == 5  # untouched on failure


# --- a full snapshot rendering every new item at once ---------------------------


def test_new_menu_items_render_from_a_single_ping_derived_snapshot(fake_rumps):
    app = kokoro_menubar.KokoroMenuBarApp()
    entries = ({"id": "1", "text": "first line"}, {"id": "2", "text": "second line"})
    snap = dataclasses.replace(
        app._snapshot,
        has_config=True,
        status="warm",
        fmt="ogg",
        held=True,
        queued=2,
        history_count=2,
        history=entries,
        stamp=1.0,
    )
    app._render(snap)

    assert app._format.hidden is False
    assert app._format_items["ogg"].state == 1
    assert app._hold.state == 1
    assert app._hold.title == "Hold output (2 queued)"
    assert app._history_items[0].hidden is False
    assert app._history_items[0].title == "first line"
    assert app._history_items[1].title == "second line"
    assert app._history_empty.hidden is True
    assert app._clear_history.callback is not None


# --- _poll_worker's conditional history refetch ---------------------------------


def test_poll_worker_refetches_history_only_when_the_count_changed(monkeypatch):
    app = _bare_app(_snapshot=dataclasses.replace(
        kokoro_menubar.Snapshot(
            status="warm", voice="af_sky", enabled=True, has_config=True,
            pid=1, message=None, stamp=0.0,
        ),
        history_count=3, history=({"id": "1"},),
    ))
    calls = []

    def fake_history(limit=15, **kwargs):
        calls.append(limit)
        return {"ok": True, "entries": [{"id": "2"}]}

    monkeypatch.setattr(kokoro_menubar, "history", fake_history)
    monkeypatch.setattr(
        kokoro_menubar, "ping",
        lambda *a, **k: {"ok": True, "pid": 1, "voice": "af_sky", "history_count": 3},
    )

    app._poll_worker()
    assert calls == []
    assert app._snapshot.history == ({"id": "1"},)

    monkeypatch.setattr(
        kokoro_menubar, "ping",
        lambda *a, **k: {"ok": True, "pid": 1, "voice": "af_sky", "history_count": 4},
    )
    app._poll_worker()
    assert calls == [15]
    assert app._snapshot.history == ({"id": "2"},)


def test_poll_worker_carries_forward_the_previous_daemon_fields_when_ping_fails(monkeypatch):
    """An unreachable daemon must not flash zeros over a value the last
    successful ping actually reported."""
    app = _bare_app(_snapshot=dataclasses.replace(
        kokoro_menubar.Snapshot(
            status="warm", voice="af_sky", enabled=True, has_config=True,
            pid=1, message=None, stamp=0.0,
        ),
        held=True, queued=2, history_count=3, history_bytes=900, history=({"id": "1"},),
    ))
    monkeypatch.setattr(kokoro_menubar, "ping", lambda *a, **k: None)

    app._poll_worker()

    assert app._snapshot.held is True
    assert app._snapshot.queued == 2
    assert app._snapshot.history_count == 3
    assert app._snapshot.history_bytes == 900
    assert app._snapshot.history == ({"id": "1"},)


def test_poll_worker_reads_the_new_ping_fields_via_get_with_defaults(monkeypatch):
    """An older daemon that predates these fields must not raise or crash the
    poll: every read goes through .get(..., default), never [...]."""
    app = _bare_app()
    monkeypatch.setattr(
        kokoro_menubar, "ping", lambda *a, **k: {"ok": True, "pid": 1, "voice": "af_heart"}
    )

    app._poll_worker()

    assert app._snapshot.held is False
    assert app._snapshot.queued == 0
    assert app._snapshot.history_count == 0
    assert app._snapshot.history_bytes == 0
