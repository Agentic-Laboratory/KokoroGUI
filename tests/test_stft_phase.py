"""The vocoder's phase is pinned at the branch cut, so backends agree.

torch.angle is discontinuous where the imaginary part is zero and the real part
is negative. MPS's FFT returns -0.0 where CPU returns +0.0, putting the two on
opposite sides of that cut: CPU yields +pi and MPS yields -pi for the same
input. About 11% of time-frequency bins differ by 2*pi as a result, and kokoro
feeds those raw values straight into the vocoder, which made Apple Silicon
output roughly 0.4 dB quieter than every other backend.

These tests drive the mask directly with a crafted spectrum rather than relying
on a particular device's signed-zero behaviour, so they assert the correction
itself and give the same verdict on CPU, MPS and CUDA.
"""
import math
from types import SimpleNamespace

import pytest
import torch

import kokoro_engine


def _stub_pipeline():
    """A pipeline shaped like kokoro's, carrying only what the patch touches."""
    stft = SimpleNamespace(
        filter_length=16,
        hop_length=4,
        win_length=16,
        window=torch.hann_window(16),
        transform=lambda input_data: ("unpatched", "unpatched"),
    )
    generator = SimpleNamespace(stft=stft)
    return SimpleNamespace(model=SimpleNamespace(decoder=SimpleNamespace(generator=generator)))


@pytest.fixture
def crafted_spectrum(monkeypatch):
    """Force torch.stft to return bins that straddle the branch cut."""
    spectrum = torch.tensor([[
        complex(-1.0, -0.0),  # MPS's signed zero: raw angle is -pi
        complex(-1.0, 0.0),   # CPU's signed zero: raw angle is +pi
        complex(1.0, 1.0),    # ordinary bin, must be left alone
        complex(-1.0, -0.5),  # negative real but imag != 0, must be left alone
    ]])
    monkeypatch.setattr(kokoro_engine.torch, "stft", lambda *a, **k: spectrum)
    return spectrum


def test_patch_reports_success_on_a_well_formed_pipeline():
    assert kokoro_engine.patch_stft_phase(_stub_pipeline()) is True


def test_patch_replaces_the_transform():
    pipeline = _stub_pipeline()
    stft = pipeline.model.decoder.generator.stft
    original = stft.transform

    kokoro_engine.patch_stft_phase(pipeline)

    assert stft.transform is not original


def test_signed_zero_bins_are_pinned_to_positive_pi(crafted_spectrum):
    pipeline = _stub_pipeline()
    kokoro_engine.patch_stft_phase(pipeline)

    _, phase = pipeline.model.decoder.generator.stft.transform(torch.zeros(64))

    # Without the patch this bin is -pi, which is what MPS produced.
    assert phase[0][0].item() == pytest.approx(math.pi)
    assert phase[0][1].item() == pytest.approx(math.pi)


def test_the_raw_angle_really_does_disagree_without_the_patch(crafted_spectrum):
    """Guards against the mask silently becoming a no-op."""
    raw = torch.angle(crafted_spectrum)

    assert raw[0][0].item() == pytest.approx(-math.pi)
    assert raw[0][1].item() == pytest.approx(math.pi)


def test_bins_away_from_the_branch_cut_are_untouched(crafted_spectrum):
    pipeline = _stub_pipeline()
    kokoro_engine.patch_stft_phase(pipeline)

    _, phase = pipeline.model.decoder.generator.stft.transform(torch.zeros(64))
    raw = torch.angle(crafted_spectrum)

    assert phase[0][2].item() == pytest.approx(raw[0][2].item())
    assert phase[0][3].item() == pytest.approx(raw[0][3].item())


def test_magnitude_is_unchanged(crafted_spectrum):
    pipeline = _stub_pipeline()
    kokoro_engine.patch_stft_phase(pipeline)

    magnitude, _ = pipeline.model.decoder.generator.stft.transform(torch.zeros(64))

    assert torch.allclose(magnitude, torch.abs(crafted_spectrum))


def test_patching_twice_does_not_wrap_twice(crafted_spectrum):
    pipeline = _stub_pipeline()
    kokoro_engine.patch_stft_phase(pipeline)
    once = pipeline.model.decoder.generator.stft.transform

    assert kokoro_engine.patch_stft_phase(pipeline) is True
    assert pipeline.model.decoder.generator.stft.transform is once


def test_a_vocoder_without_the_expected_seam_is_left_alone():
    """A kokoro upgrade that moves the seam must degrade, not crash."""
    assert kokoro_engine.patch_stft_phase(SimpleNamespace(model=None)) is False
    assert kokoro_engine.patch_stft_phase(SimpleNamespace()) is False
