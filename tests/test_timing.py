"""Tests for detection-timing estimation. analyze() is pure (given a
seeded RNG and fixed `now`), so these use synthetic sample series with
known arrival distributions."""

from __future__ import annotations

import random

from uc_browser.timing import DETECT_GATE, analyze, sample_load


def _load(cross_at_ms, interval=500, horizon=5000, peak=8.0):
    """A single load's samples: 0 until cross_at_ms, then `peak`."""
    return [(t, 0.0 if t < cross_at_ms else peak)
            for t in range(0, horizon + 1, interval)]


def test_arrival_mean_and_ci():
    # Composer appears at 1000,1500,1000,1500,1000 ms across 5 loads.
    loads = [_load(t) for t in (1000, 1500, 1000, 1500, 1000)]
    tp = analyze("x", loads, interval_ms=500, horizon_ms=5000,
                 rng=random.Random(0), now=1000.0)
    assert tp.reliability == 1.0
    assert tp.appear_mean_ms == 1200.0        # mean of the five
    assert tp.appear_ci95_ms is not None
    lo, hi = tp.appear_ci95_ms
    assert lo < 1200.0 < hi                    # CI brackets the mean
    # Recommended wait covers the slow tail + a margin.
    assert tp.recommended_wait_ms >= hi


def test_capture_window_and_variance():
    loads = [_load(1000, peak=8.0) for _ in range(4)]
    tp = analyze("x", loads, interval_ms=500, horizon_ms=5000,
                 rng=random.Random(0), now=1.0)
    assert tp.capture_window_ms is not None
    lo, hi = tp.capture_window_ms
    assert lo >= 1000 and hi == 5000
    # Stable composer → near-zero score variance in the window.
    assert tp.score_variance == 0.0
    assert tp.score_mean == 8.0


def test_flaky_site_high_variance_low_reliability():
    # Only 2 of 5 loads ever show the composer → reliability 0.4.
    loads = [_load(1000), _load(1000),
             _load(9999, horizon=5000), _load(9999, horizon=5000),
             _load(9999, horizon=5000)]
    tp = analyze("flaky", loads, interval_ms=500, horizon_ms=5000,
                 rng=random.Random(0), now=1.0)
    assert tp.reliability == 0.4
    # Majority-present window may not exist (only 40% ever detect).
    assert tp.capture_window_ms is None


def test_never_detected():
    loads = [_load(9999, horizon=5000) for _ in range(3)]
    tp = analyze("dead", loads, interval_ms=500, horizon_ms=5000,
                 rng=random.Random(0), now=1.0)
    assert tp.reliability == 0.0
    assert tp.appear_mean_ms is None
    assert tp.recommended_wait_ms is None


def test_sample_load_early_stops_when_stable():
    """sample_load polls a fake page/detector and stops once the score
    holds >= gate for 2 samples."""
    class FakePage:
        def wait_for_timeout(self, ms):
            pass
        def close(self):
            pass

    scores = iter([0.0, 0.0, 6.0, 6.0, 6.0, 6.0])  # crosses at 3rd sample

    def detect(_page):
        return next(scores)

    samples = sample_load(lambda: FakePage(), detect,
                          interval_ms=100, horizon_ms=2000, gate=DETECT_GATE,
                          early_stop_stable=2)
    # 0,0,6,6 -> two stable >=gate samples, stop at the 4th.
    assert [s for _, s in samples] == [0.0, 0.0, 6.0, 6.0]
