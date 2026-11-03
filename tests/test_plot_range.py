"""Live plots keep a steady y range, so axes are not redrawn every frame."""
import os

import pytest


@pytest.fixture(scope="module")
def sticky_range():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6.QtWidgets")
    from shimmer_live.ui.live_page import sticky_range
    return sticky_range


def test_grows_at_once_shrinks_only_when_much_smaller(sticky_range):
    r = sticky_range(None, 0.0, 10.0)
    assert r[0] < 0 and r[1] > 10
    assert sticky_range(r, 1.0, 9.0) is None                 # still fits, not much smaller: keep
    assert sticky_range(r, 0.0, 12.0)[1] > 12                 # new peak: grow now
    assert sticky_range(r, 4.0, 5.0) is not None              # uses < half of the range: shrink


def test_flat_signal_gets_a_visible_range(sticky_range):
    lo, hi = sticky_range(None, 605.0, 605.0)
    assert lo < 605.0 < hi
