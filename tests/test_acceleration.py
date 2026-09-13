"""Tests for dynamic CPU, CUDA, ROCm, MPS, and XPU device selection."""
import asyncio
from types import SimpleNamespace

import kokoro_engine


def test_get_inference_device_falls_back_to_cpu(monkeypatch):
    monkeypatch.setattr(kokoro_engine.torch.cuda, "is_available", lambda: False)
    monkeypatch.setattr(kokoro_engine.torch.backends, "mps", SimpleNamespace(is_available=lambda: False), raising=False)
    monkeypatch.setattr(kokoro_engine.torch, "xpu", SimpleNamespace(is_available=lambda: False), raising=False)

    assert kokoro_engine.get_inference_device() == ("cpu", "CPU")


def test_get_inference_device_identifies_rocm_gpu(monkeypatch):
    monkeypatch.setattr(kokoro_engine.torch.cuda, "is_available", lambda: True)
    monkeypatch.setattr(kokoro_engine.torch.cuda, "get_device_name", lambda index: "AMD Radeon RX 6900 XT")
    monkeypatch.setattr(kokoro_engine.torch.version, "hip", "7.1")

    assert kokoro_engine.get_inference_device() == ("cuda", "ROCm GPU: AMD Radeon RX 6900 XT")


def test_get_inference_device_identifies_nvidia_gpu(monkeypatch):
    monkeypatch.setattr(kokoro_engine.torch.cuda, "is_available", lambda: True)
    monkeypatch.setattr(kokoro_engine.torch.cuda, "get_device_name", lambda index: "NVIDIA RTX 5090")
    monkeypatch.setattr(kokoro_engine.torch.version, "hip", None)

    assert kokoro_engine.get_inference_device() == ("cuda", "CUDA GPU: NVIDIA RTX 5090")


def test_get_inference_device_identifies_apple_silicon(monkeypatch):
    monkeypatch.setattr(kokoro_engine.torch.cuda, "is_available", lambda: False)
    monkeypatch.setattr(kokoro_engine.torch.backends, "mps", SimpleNamespace(is_available=lambda: True), raising=False)

    assert kokoro_engine.get_inference_device() == ("mps", "Apple Silicon GPU")


def test_get_inference_device_identifies_intel_xpu(monkeypatch):
    monkeypatch.setattr(kokoro_engine.torch.cuda, "is_available", lambda: False)
    monkeypatch.setattr(kokoro_engine.torch.backends, "mps", SimpleNamespace(is_available=lambda: False), raising=False)
    monkeypatch.setattr(
        kokoro_engine.torch,
        "xpu",
        SimpleNamespace(is_available=lambda: True, get_device_name=lambda index: "Intel Arc B580"),
        raising=False,
    )

    assert kokoro_engine.get_inference_device() == ("xpu", "Intel XPU: Intel Arc B580")


def test_thread_pipeline_uses_detected_device(monkeypatch):
    calls = []

    def make_pipeline(lang_code, device):
        calls.append((lang_code, device))
        return type("Pipeline", (), {"lang_code": lang_code})()

    monkeypatch.delattr(kokoro_engine.thread_local, "pipeline", raising=False)
    monkeypatch.setattr(kokoro_engine, "KPipeline", make_pipeline)
    monkeypatch.setattr(kokoro_engine, "get_inference_device", lambda: ("cuda", "ROCm GPU: test"))

    kokoro_engine.get_thread_pipeline("a")

    assert calls == [("a", "cuda")]


def test_pipeline_initialization_reports_selected_device(engine, monkeypatch):
    class Pipeline:
        pass

    monkeypatch.setattr(kokoro_engine, "KPipeline", lambda lang_code, device: Pipeline())
    monkeypatch.setattr(kokoro_engine, "get_inference_device", lambda: ("cuda", "ROCm GPU: test"))
    statuses = []
    engine.on_status = lambda message, is_error: statuses.append((message, is_error))

    assert asyncio.run(engine.init_pipeline_async("a"))
    assert statuses == [("Pipeline Initialized (a, ROCm GPU: test).", False)]
