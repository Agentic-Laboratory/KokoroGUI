"""Tests for the sounddevice playback lifecycle."""
from unittest.mock import MagicMock

import playback


class ImmediateThread:
    """Runs its target synchronously so the test has no background work."""

    def __init__(self, target, daemon):
        self.target = target
        self.daemon = daemon

    def start(self):
        self.target()


def test_nonblocking_play_waits_in_daemon_thread(monkeypatch):
    sounddevice = MagicMock()
    monkeypatch.setattr(playback, "AVAILABLE", True)
    monkeypatch.setattr(playback, "sd", sounddevice)
    monkeypatch.setattr(playback.sf, "read", MagicMock(return_value=("audio", 24000)))
    monkeypatch.setattr(playback.threading, "Thread", ImmediateThread)

    playback.play("preview.wav")

    playback.sf.read.assert_called_once_with("preview.wav", dtype="float32")
    sounddevice.play.assert_called_once_with("audio", 24000)
    sounddevice.wait.assert_called_once_with()


def test_blocking_play_waits_without_starting_thread(monkeypatch):
    sounddevice = MagicMock()
    thread = MagicMock()
    monkeypatch.setattr(playback, "AVAILABLE", True)
    monkeypatch.setattr(playback, "sd", sounddevice)
    monkeypatch.setattr(playback.sf, "read", MagicMock(return_value=("audio", 24000)))
    monkeypatch.setattr(playback.threading, "Thread", thread)

    playback.play("chunk.wav", blocking=True)

    sounddevice.play.assert_called_once_with("audio", 24000)
    sounddevice.wait.assert_called_once_with()
    thread.assert_not_called()
