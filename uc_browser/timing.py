"""Detection-timing estimation — learn WHEN a chat composer becomes
detectable instead of guessing a fixed wait.

The problem: widgets hydrate / lazy-load / inject-on-open at times that
vary load to load, so a single ``wait_for_timeout(2500)`` samples the
page at an arbitrary instant and misses composers that arrive late (the
launcher-open and lazy-load recall gaps).

The method (two phases, per the sampling design):

  Phase 1 — LEARN THE ARRIVAL TIME.
    Sample the detector score every N ms across the page's load, for K
    independent loads. Each load yields a first-crossing time t_k (when
    the score first clears the gate). {t_1..t_K} is a sample from the
    arrival-time distribution; report its mean and a t-based 95%
    confidence interval, plus the CAPTURE WINDOW — the interval where the
    composer is present on a majority of loads.

  Phase 2 — CHARACTERIZE THE SIGNAL.
    Within the capture window, simple-random-sample the score across the
    loads to estimate its variance and reliability (fraction of loads
    that ever detected). High variance flags a flaky widget (the klarna
    4.2-vs-0.0 case); the mean arrival time + margin becomes the learned
    wait future probes use.

Output is a TimingProfile, stored on the SiteProfile so the intelligence
persists and every later probe / generic send waits the learned time.
"""

from __future__ import annotations

import logging
import random
import statistics
import time
from dataclasses import asdict, dataclass
from typing import Callable, Optional

logger = logging.getLogger("uc_browser.timing")

DEFAULT_INTERVAL_MS = 500
DEFAULT_HORIZON_MS = 20000
DEFAULT_LOADS = 5
DETECT_GATE = 4.0

# Two-sided 95% Student-t critical values by degrees of freedom (df=K-1).
_T95 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447,
        7: 2.365, 8: 2.306, 9: 2.262, 10: 2.228, 12: 2.179, 15: 2.131,
        20: 2.086, 25: 2.060, 30: 2.042}


def _t_critical(df: int) -> float:
    if df <= 0:
        return float("nan")
    if df in _T95:
        return _T95[df]
    if df > 30:
        return 1.96                      # normal approximation
    # nearest tabulated df below
    keys = [k for k in _T95 if k <= df]
    return _T95[max(keys)] if keys else 1.96


@dataclass
class TimingProfile:
    site: str
    n_loads: int
    sample_interval_ms: int
    horizon_ms: int
    reliability: float                    # fraction of loads that detected
    appear_mean_ms: Optional[float]       # mean first-crossing time
    appear_std_ms: Optional[float]
    appear_ci95_ms: Optional[list]        # [lo, hi] on the MEAN
    capture_window_ms: Optional[list]     # [lo, hi] where present on majority
    score_mean: Optional[float]           # SRS mean score in the window
    score_variance: Optional[float]       # SRS variance in the window
    recommended_wait_ms: Optional[int]    # what future probes should wait
    at: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


def _first_crossing(samples: list[tuple], gate: float) -> Optional[float]:
    """First sample time (ms) whose score >= gate, or None."""
    for t_ms, score in samples:
        if score >= gate:
            return t_ms
    return None


def analyze(site: str, loads: list[list[tuple]], *,
            interval_ms: int, horizon_ms: int, gate: float = DETECT_GATE,
            srs_n: int = 40, rng: Optional[random.Random] = None,
            now: Optional[float] = None) -> TimingProfile:
    """Compute a TimingProfile from K loads of (t_ms, score) samples.

    Pure/deterministic given ``rng`` — no I/O — so it is unit-testable
    with synthetic sample series.
    """
    rng = rng or random.Random(0)
    K = len(loads)

    # Phase 1: arrival-time distribution.
    crossings = [c for c in (_first_crossing(s, gate) for s in loads)
                 if c is not None]
    reliability = len(crossings) / K if K else 0.0
    appear_mean = appear_std = None
    appear_ci = None
    if crossings:
        appear_mean = statistics.mean(crossings)
        if len(crossings) >= 2:
            appear_std = statistics.stdev(crossings)
            se = appear_std / (len(crossings) ** 0.5)
            h = _t_critical(len(crossings) - 1) * se
            appear_ci = [round(appear_mean - h, 1), round(appear_mean + h, 1)]

    # Capture window: contiguous times where a MAJORITY of loads are >= gate.
    grid = list(range(0, horizon_ms + 1, interval_ms))
    def present_frac(t):
        vals = []
        for s in loads:
            # score at-or-before t (last known), else 0
            v = 0.0
            for tt, sc in s:
                if tt <= t:
                    v = sc
                else:
                    break
            vals.append(1.0 if v >= gate else 0.0)
        return sum(vals) / K if K else 0.0
    present = [t for t in grid if present_frac(t) > 0.5]
    capture_window = [min(present), max(present)] if present else None

    # Phase 2: SRS of the score within the capture window → variance.
    score_mean = score_var = None
    if capture_window:
        lo, hi = capture_window
        pool = [sc for s in loads for (tt, sc) in s if lo <= tt <= hi]
        if pool:
            draw = pool if len(pool) <= srs_n else rng.sample(pool, srs_n)
            score_mean = statistics.mean(draw)
            score_var = statistics.pvariance(draw) if len(draw) >= 1 else 0.0

    # Recommended wait: upper CI of arrival (covers slow loads) + one
    # interval of margin; fall back to capture-window end or horizon.
    rec = None
    if appear_ci:
        rec = int(appear_ci[1] + interval_ms)
    elif appear_mean is not None:
        rec = int(appear_mean + interval_ms)
    elif capture_window:
        rec = int(capture_window[0] + interval_ms)
    if rec is not None:
        rec = max(interval_ms, min(rec, horizon_ms))

    return TimingProfile(
        site=site, n_loads=K, sample_interval_ms=interval_ms,
        horizon_ms=horizon_ms, reliability=round(reliability, 3),
        appear_mean_ms=round(appear_mean, 1) if appear_mean is not None else None,
        appear_std_ms=round(appear_std, 1) if appear_std is not None else None,
        appear_ci95_ms=appear_ci, capture_window_ms=capture_window,
        score_mean=round(score_mean, 3) if score_mean is not None else None,
        score_variance=round(score_var, 4) if score_var is not None else None,
        recommended_wait_ms=rec, at=now if now is not None else time.time(),
    )


def sample_load(open_page: Callable, detect: Callable, *,
                interval_ms: int = DEFAULT_INTERVAL_MS,
                horizon_ms: int = DEFAULT_HORIZON_MS,
                gate: float = DETECT_GATE,
                early_stop_stable: int = 2) -> list[tuple]:
    """Drive ONE load: open a page, then poll ``detect(page)`` every
    ``interval_ms`` up to ``horizon_ms``, returning [(t_ms, score)].

    ``open_page()`` returns a navigated page; ``detect(page)`` returns a
    float score. Stops early once the score has held >= gate for
    ``early_stop_stable`` consecutive samples (the composer has settled).
    """
    page = open_page()
    samples: list[tuple] = []
    stable = 0
    try:
        t = 0
        while t <= horizon_ms:
            try:
                score = float(detect(page))
            except Exception:
                score = 0.0
            samples.append((t, round(score, 2)))
            stable = stable + 1 if score >= gate else 0
            if stable >= early_stop_stable:
                break
            page.wait_for_timeout(interval_ms)
            t += interval_ms
    finally:
        try:
            page.close()
        except Exception:
            pass
    return samples


def make_commit_opener(ctx, url: str, *, timeout_ms: int = 40000,
                       on_open: Optional[Callable] = None) -> Callable:
    """Build an ``open_page`` that navigates with wait_until='commit' so it
    returns BEFORE hydration — the sampling clock then starts near t=0 and
    captures the composer's actual arrival curve. (Opening at
    'domcontentloaded' on a warm cache samples 'already present' and
    collapses arrival to 0.) ``on_open(page)`` runs once post-navigation
    for setup like consent dismissal."""
    def _open():
        page = ctx.new_page()
        try:
            page.goto(url, timeout=timeout_ms, wait_until="commit")
        except Exception:
            pass
        if on_open:
            try:
                on_open(page)
            except Exception:
                pass
        return page
    return _open


def learn_timing(site: str, open_page: Callable, detect: Callable, *,
                 loads: int = DEFAULT_LOADS,
                 interval_ms: int = DEFAULT_INTERVAL_MS,
                 horizon_ms: int = DEFAULT_HORIZON_MS,
                 gate: float = DETECT_GATE) -> TimingProfile:
    """Full calibration: K sampled loads → TimingProfile. Expensive (K
    navigations); run occasionally to populate a profile, then reuse
    ``recommended_wait_ms`` on ordinary probes."""
    all_samples = []
    for k in range(loads):
        s = sample_load(open_page, detect, interval_ms=interval_ms,
                        horizon_ms=horizon_ms, gate=gate)
        all_samples.append(s)
        logger.info("timing[%s] load %d/%d: crossing=%s",
                    site, k + 1, loads, _first_crossing(s, gate))
    return analyze(site, all_samples, interval_ms=interval_ms,
                   horizon_ms=horizon_ms, gate=gate)
