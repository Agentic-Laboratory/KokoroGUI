"""Tests for sounddevice's lazy PortAudio lifecycle."""
from unittest.mock import MagicMock

import pytest

import playback


class ImmediateThread:
    """Runs its target synchronously so the test has no background work."""

    def __init__(self, target, args, daemon):
        self.target = target
        self.args = args
        self.daemon = daemon

    def start(self):
        self.target(*self.args)


def configure_backend(monkeypatch, backend):
    monkeypatch.setattr(playback, "_sounddevice", backend)
    monkeypatch.setattr(playback, "_load_attempted", True)
    monkeypatch.setattr(playback, "_active_backend", None)
    monkeypatch.setattr(playback, "_active_process", None)
    monkeypatch.setattr(playback, "_use_aplay", lambda path: False)


def test_nonblocking_play_releases_portaudio_in_daemon_thread(monkeypatch):
    backend = MagicMock()
    configure_backend(monkeypatch, backend)
    monkeypatch.setattr(playback.sf, "read", MagicMock(return_value=("audio", 24000)))
    monkeypatch.setattr(playback.threading, "Thread", ImmediateThread)

    playback.play("preview.wav")

    playback.sf.read.assert_called_once_with("preview.wav", dtype="float32")
    backend._initialize.assert_called_once_with()
    backend.play.assert_called_once_with("audio", 24000)
    backend.wait.assert_called_once_with()
    backend.stop.assert_called_once_with()
    backend._terminate.assert_called_once_with()


def test_blocking_play_releases_portaudio_without_starting_thread(monkeypatch):
    backend = MagicMock()
    thread = MagicMock()
    configure_backend(monkeypatch, backend)
    monkeypatch.setattr(playback.sf, "read", MagicMock(return_value=("audio", 24000)))
    monkeypatch.setattr(playback.threading, "Thread", thread)

    playback.play("chunk.wav", blocking=True)

    backend._initialize.assert_called_once_with()
    backend.play.assert_called_once_with("audio", 24000)
    backend.wait.assert_called_once_with()
    backend.stop.assert_called_once_with()
    backend._terminate.assert_called_once_with()
    thread.assert_not_called()


def test_stop_does_not_load_an_audio_backend(monkeypatch):
    importer = MagicMock()
    monkeypatch.setattr(playback, "_sounddevice", None)
    monkeypatch.setattr(playback, "_load_attempted", False)
    monkeypatch.setattr(playback, "_active_process", None)
    monkeypatch.setattr(playback.importlib, "import_module", importer)

    playback.stop()

    importer.assert_not_called()


def test_play_failure_still_terminates_portaudio(monkeypatch):
    backend = MagicMock()
    backend.play.side_effect = RuntimeError("device unavailable")
    configure_backend(monkeypatch, backend)
    monkeypatch.setattr(playback.sf, "read", MagicMock(return_value=("audio", 24000)))

    with pytest.raises(RuntimeError, match="device unavailable"):
        playback.play("broken.wav", blocking=True)

    backend.stop.assert_called_once_with()
    backend._terminate.assert_called_once_with()


def test_linux_playback_uses_pipewire_aplay(monkeypatch):
    process = MagicMock()
    process.returncode = 0
    process.communicate.return_value = ("", "")
    monkeypatch.setattr(playback, "_active_backend", None)
    monkeypatch.setattr(playback, "_active_process", None)
    monkeypatch.setattr(playback, "_use_aplay", lambda path: True)
    monkeypatch.setattr(playback.subprocess, "Popen", MagicMock(return_value=process))

    playback.play("preview.wav", blocking=True)

    playback.subprocess.Popen.assert_called_once_with(
        ["aplay", "--quiet", "--device=pipewire", "preview.wav"],
        stdout=playback.subprocess.DEVNULL,
        stderr=playback.subprocess.PIPE,
        text=True,
    )
    process.communicate.assert_called_once_with()


def test_use_aplay_gates_on_the_wav_extension_case_insensitively(monkeypatch):
    monkeypatch.setattr(playback.sys, "platform", "linux")
    monkeypatch.setattr(playback.shutil, "which", lambda name: "/usr/bin/aplay")

    assert playback._use_aplay("line.wav") is True
    assert playback._use_aplay("line.WAV") is True
    assert playback._use_aplay("line.ogg") is False
    assert playback._use_aplay("line.mp3") is False


def test_ogg_on_linux_falls_through_to_the_sounddevice_backend(monkeypatch):
    backend = MagicMock()
    monkeypatch.setattr(playback, "_sounddevice", backend)
    monkeypatch.setattr(playback, "_load_attempted", True)
    monkeypatch.setattr(playback, "_active_backend", None)
    monkeypatch.setattr(playback, "_active_process", None)
    monkeypatch.setattr(playback.sys, "platform", "linux")
    monkeypatch.setattr(playback.shutil, "which", lambda name: "/usr/bin/aplay")
    monkeypatch.setattr(playback.sf, "read", MagicMock(return_value=("audio", 24000)))
    popen = MagicMock()
    monkeypatch.setattr(playback.subprocess, "Popen", popen)

    playback.play("line.ogg", blocking=True)

    popen.assert_not_called()
    backend.play.assert_called_once_with("audio", 24000)
