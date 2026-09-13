"""Cross-platform audio playback for preview buttons and JIT streaming.

PortAudio is initialized only while audio is playing.  Importing sounddevice
initializes PortAudio immediately, which can otherwise reserve an audio
backend for the application's entire lifetime.
"""
import importlib
import logging
import shutil
import subprocess
import sys
import threading

import soundfile as sf


logger = logging.getLogger(__name__)
_sounddevice = None
_load_attempted = False
_active_backend = None
_active_process = None
_request_id = 0
_state_lock = threading.Lock()
_backend_lock = threading.Lock()
_playback_lock = threading.Lock()


def _use_aplay() -> bool:
    """Use ALSA's PipeWire endpoint when it is available on Linux."""
    return sys.platform.startswith("linux") and shutil.which("aplay") is not None


def _load_sounddevice():
    """Load sounddevice without leaving its import-time PortAudio client open."""
    global _sounddevice, _load_attempted

    with _backend_lock:
        if _load_attempted:
            logger.debug("Audio backend already loaded: available=%s", _sounddevice is not None)
            return _sounddevice

        _load_attempted = True
        try:
            logger.debug("Loading sounddevice audio backend")
            backend = importlib.import_module("sounddevice")
            # sounddevice initializes PortAudio during import. Release that
            # client now; each playback request initializes it when needed.
            backend._terminate()
        except (ImportError, OSError):
            logger.exception("Unable to load sounddevice audio backend")
            return None

        _sounddevice = backend
        logger.debug("Sounddevice loaded and import-time PortAudio client released")
        return backend


def _is_current(request_id: int) -> bool:
    with _state_lock:
        return request_id == _request_id


def _play(path: str, request_id: int) -> None:
    """Play one request and release PortAudio before returning."""
    global _active_backend

    backend = _load_sounddevice()
    if backend is None:
        logger.error("Audio request %s skipped because no backend is available", request_id)
        return

    with _playback_lock:
        if not _is_current(request_id):
            logger.debug("Audio request %s discarded before loading file", request_id)
            return

        logger.debug("Audio request %s reading %s", request_id, path)
        data, samplerate = sf.read(path, dtype="float32")
        if not _is_current(request_id):
            logger.debug("Audio request %s discarded after loading file", request_id)
            return

        initialized = False
        try:
            logger.debug("Audio request %s initializing PortAudio at %s Hz", request_id, samplerate)
            backend._initialize()
            initialized = True

            with _state_lock:
                if request_id != _request_id:
                    logger.debug("Audio request %s cancelled during initialization", request_id)
                    return
                _active_backend = backend

            logger.debug("Audio request %s starting stream", request_id)
            backend.play(data, samplerate)
            logger.debug("Audio request %s waiting for stream completion", request_id)
            backend.wait()
            logger.debug("Audio request %s stream completed", request_id)
        finally:
            with _state_lock:
                if _active_backend is backend:
                    _active_backend = None

            if initialized:
                try:
                    logger.debug("Audio request %s stopping stream", request_id)
                    backend.stop()
                finally:
                    logger.debug("Audio request %s terminating PortAudio", request_id)
                    backend._terminate()


def _play_with_aplay(path: str, request_id: int) -> None:
    """Play a WAV through PipeWire without starting a PortAudio client."""
    global _active_process

    with _playback_lock:
        if not _is_current(request_id):
            logger.debug("Audio request %s discarded before starting aplay", request_id)
            return

        logger.debug("Audio request %s starting aplay through PipeWire", request_id)
        process = subprocess.Popen(
            ["aplay", "--quiet", "--device=pipewire", path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )

        with _state_lock:
            if request_id != _request_id:
                process.terminate()
                process.wait()
                return
            _active_process = process

        try:
            _, stderr = process.communicate()
            if process.returncode:
                logger.error(
                    "Audio request %s failed through aplay (exit %s): %s",
                    request_id,
                    process.returncode,
                    stderr.strip(),
                )
            else:
                logger.debug("Audio request %s completed through aplay", request_id)
        finally:
            with _state_lock:
                if _active_process is process:
                    _active_process = None


def play(path: str, blocking: bool = False) -> None:
    """Play an audio file, replacing any queued or active playback."""
    global _request_id

    with _state_lock:
        _request_id += 1
        request_id = _request_id
        active_backend = _active_backend
        active_process = _active_process

    logger.debug("Queued audio request %s for %s (blocking=%s)", request_id, path, blocking)
    if active_backend is not None:
        logger.debug("Stopping active audio before request %s", request_id)
        active_backend.stop()
    if active_process is not None:
        logger.debug("Stopping active aplay process before request %s", request_id)
        active_process.terminate()

    player = _play_with_aplay if _use_aplay() else _play
    if blocking:
        player(path, request_id)
    else:
        threading.Thread(target=player, args=(path, request_id), daemon=True).start()


def stop() -> None:
    """Stop active playback without initializing an audio backend."""
    global _request_id

    with _state_lock:
        _request_id += 1
        active_backend = _active_backend
        active_process = _active_process

    if active_backend is not None:
        logger.debug("Stopping active audio request")
        active_backend.stop()
    if active_process is not None:
        logger.debug("Stopping active aplay process")
        active_process.terminate()
    if active_backend is None and active_process is None:
        logger.debug("Audio stop requested with no active stream")
