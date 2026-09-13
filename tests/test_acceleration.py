"""Tests for dynamic CPU, CUDA, ROCm, MPS, and XPU device selection."""
import asyncio
from types import SimpleNamespace
import warnings

import kokoro_engine


def test_native_runtime_logging_hides_miopen_warnings_by_default(monkeypatch):
    monkeypatch.delenv("MIOPEN_LOG_LEVEL", raising=False)

    kokoro_engine.configure_native_runtime_logging()

    assert kokoro_engine.os.environ["MIOPEN_LOG_LEVEL"] == "3"


def test_native_runtime_logging_preserves_user_miopen_log_level(monkeypatch):
    monkeypatch.setenv("MIOPEN_LOG_LEVEL", "6")

    kokoro_engine.configure_native_runtime_logging()

    assert kokoro_engine.os.environ["MIOPEN_LOG_LEVEL"] == "6"


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

    def make_pipeline(lang_code, device, repo_id):
        calls.append((lang_code, device, repo_id))
        return type("Pipeline", (), {"lang_code": lang_code})()

    monkeypatch.delattr(kokoro_engine.thread_local, "pipeline", raising=False)
    monkeypatch.setattr(kokoro_engine, "KPipeline", make_pipeline)
    monkeypatch.setattr(kokoro_engine, "get_inference_device", lambda: ("cuda", "ROCm GPU: test"))

    kokoro_engine.get_thread_pipeline("a")

    assert calls == [("a", "cuda", kokoro_engine.KOKORO_REPO_ID)]


def test_pipeline_initialization_reports_selected_device(engine, monkeypatch):
    class Pipeline:
        pass

    monkeypatch.setattr(kokoro_engine, "KPipeline", lambda lang_code, device, repo_id: Pipeline())
    monkeypatch.setattr(kokoro_engine, "get_inference_device", lambda: ("cuda", "ROCm GPU: test"))
    statuses = []
    engine.on_status = lambda message, is_error: statuses.append((message, is_error))

    assert asyncio.run(engine.init_pipeline_async("a"))
    assert statuses == [("Pipeline Initialized (a, ROCm GPU: test).", False)]


def test_runtime_warning_filters_only_hide_known_kokoro_compatibility_messages():
    with warnings.catch_warnings(record=True) as captured:
        warnings.resetwarnings()
        kokoro_engine.configure_runtime_warnings()
        known_warnings = [
            (
                "dropout option adds dropout after all but last recurrent layer, so non-zero dropout expects num_layers greater than 1",
                UserWarning,
                "torch.nn.modules.rnn",
            ),
            (
                "`torch.nn.utils.weight_norm` is deprecated in favor of `torch.nn.utils.parametrizations.weight_norm`.",
                FutureWarning,
                "torch.nn.utils.weight_norm",
            ),
            (
                "`torch.jit.script` is deprecated and will be removed in a future version.",
                DeprecationWarning,
                "torch.jit._script",
            ),
        ]
        for line, (message, category, module) in enumerate(known_warnings, start=1):
            warnings.warn_explicit(message, category, filename="torch.py", lineno=line, module=module)
        warnings.warn_explicit("unrelated RNN warning", UserWarning, filename="rnn.py", lineno=4, module="torch.nn.modules.rnn")

    assert [str(warning.message) for warning in captured] == ["unrelated RNN warning"]
