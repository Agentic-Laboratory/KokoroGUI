"""Tests for the delayed GUI setting help."""


def test_tooltip_cancels_scheduled_display(tts_app):
    tooltip = tts_app._tooltips[0]

    tooltip._schedule()
    assert tooltip._after_id is not None

    tooltip._cancel()
    assert tooltip._after_id is None
    assert tooltip._window is None
