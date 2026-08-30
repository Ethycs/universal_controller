"""Site health monitoring: tiered live probes + uptime history.

Probe levels (each implies the previous):
  reach   the page loads and renders a real DOM
  detect  the generic engine finds a chat input that clears the same
          score>=4 gate ``UCBrowser.chat()`` uses to engage
  send    a real round-trip through the site's driver (costs quota;
          only sites with a driver support it — currently uc/grok).
          NOT probed by default.

Statuses:
  ok         probe reached the site's configured level
  degraded   reachable, but the composer wasn't detected
  login      reachable, but a login wall is blocking the chat UI
  down       navigation failed / DOM empty / challenge page
  unknown    never probed, or probe machinery itself failed

History: JSONL appended per probe at ``data/health/history.jsonl``;
``data/health/latest.json`` holds the newest result per site plus rolling
uptime, and is what the litellm availability gate and the status page
read. Both paths follow ``UC_HEALTH_DIR``.

The probe browser runs headful (several targets block headless) but
positioned off-screen so unattended sweeps don't steal the desktop.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from uc_browser._paths import _SUBMODULE_ROOT, ext_dir
from uc_browser.registry import SiteEntry

logger = logging.getLogger("uc_browser.health")

STATUS_OK = "ok"
STATUS_DEGRADED = "degraded"
STATUS_LOGIN = "login"
STATUS_DOWN = "down"
STATUS_UNKNOWN = "unknown"

# Statuses that count against uptime. "login" is excluded: the site is
# up, our credentials aren't — different pager.
_DOWNISH = {STATUS_DOWN, STATUS_DEGRADED}


def _health_dir() -> Path:
    override = os.environ.get("UC_HEALTH_DIR")
    if override:
        return Path(override).resolve()
    return _SUBMODULE_ROOT / "data" / "health"


@dataclass
class ProbeResult:
    site: str
    status: str
    level_reached: str            # none | reach | detect | send
    latency_ms: int
    detail: str = ""
    at: float = 0.0               # epoch seconds

    def to_dict(self) -> dict:
        return asdict(self)


# ── in-page helpers ───────────────────────────────────────────────────

_LOGIN_WALL_JS = """() => {
  const pw = document.querySelector('input[type="password"]');
  const text = (document.body ? document.body.innerText : '').toLowerCase();
  const signals = ['sign in', 'log in', 'login', 'sign up'];
  const hits = signals.filter(s => text.includes(s)).length;
  const inputs = document.querySelectorAll(
    'textarea, [contenteditable="true"], input[type="text"]').length;
  return { pw: !!pw, signalHits: hits, inputs,
           textLen: text.length, els: document.querySelectorAll('*').length };
}"""

_READY_INPUT_JS = """() => {
  const q = 'textarea, input[type="text"], input:not([type]), '
    + '[contenteditable]:not([contenteditable="false"]), [role="textbox"]';
  return [...document.querySelectorAll(q)].some(el => {
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  });
}"""

# Structural launcher discovery: chat widgets open from a fixed/sticky
# bubble in the lower half of the viewport (button or iframe) whose
# label/id/src mentions chat-ish keywords. Generic by design — no
# site-specific selectors. Returns click targets, best first.
_LAUNCHER_CANDIDATES_JS = """() => {
  const kw = /chat|message|help|support|assist|bot|talk/i;
  const out = [];
  const consider = (el, desc, bonus) => {
    const r = el.getBoundingClientRect();
    if (r.width < 24 || r.height < 24 || r.width > 420 || r.height > 220) return;
    if (r.bottom < window.innerHeight * 0.5) return;
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden' || +cs.opacity === 0) return;
    let fixed = false, cur = el;
    while (cur && cur !== document.body) {
      const s = getComputedStyle(cur);
      if (s.position === 'fixed' || s.position === 'sticky') { fixed = true; break; }
      cur = cur.parentElement;
    }
    if (!fixed) return;  // launchers float; anything in-flow is a nav link
    let score = bonus + 2;
    if (r.right > window.innerWidth * 0.7) score += 1;
    out.push({ x: r.x + r.width / 2, y: r.y + r.height / 2,
               w: Math.round(r.width), h: Math.round(r.height),
               score, desc: desc.trim().slice(0, 80) });
  };
  for (const el of document.querySelectorAll(
      'button, [role="button"], a, div[tabindex], div[onclick]')) {
    const text = (el.getAttribute('aria-label') || '') + ' ' + (el.id || '')
      + ' ' + (typeof el.className === 'string' ? el.className : '')
      + ' ' + (el.innerText || '').slice(0, 40);
    if (kw.test(text)) consider(el, 'el:' + text, 1);
  }
  for (const f of document.querySelectorAll('iframe')) {
    const text = (f.title || '') + ' ' + (f.id || '') + ' ' + (f.src || '')
      + ' ' + (f.name || '');
    if (kw.test(text)) consider(f, 'iframe:' + text, 2);
  }
  out.sort((a, b) => b.score - a.score);
  return out.slice(0, 5);
}"""


class HealthStore:
    """Append-only probe history + latest-per-site snapshot."""

    def __init__(self, base_dir: Optional[Path] = None):
        self._dir = base_dir or _health_dir()
        self._lock = threading.Lock()

    @property
    def history_path(self) -> Path:
        return self._dir / "history.jsonl"

    @property
    def latest_path(self) -> Path:
        return self._dir / "latest.json"

    def record(self, result: ProbeResult) -> None:
        with self._lock:
            self._dir.mkdir(parents=True, exist_ok=True)
            with self.history_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(result.to_dict()) + "\n")
            latest = self._read_latest()
            latest[result.site] = result.to_dict()
            tmp = self.latest_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(latest, indent=2), encoding="utf-8")
            tmp.replace(self.latest_path)

    def _read_latest(self) -> dict:
        if not self.latest_path.exists():
            return {}
        try:
            return json.loads(self.latest_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def latest(self, site: Optional[str] = None) -> dict:
        data = self._read_latest()
        if site is not None:
            return data.get(site, {})
        return data

    def history(self, site: Optional[str] = None,
                since_s: Optional[float] = None) -> list[dict]:
        if not self.history_path.exists():
            return []
        cutoff = time.time() - since_s if since_s else None
        out = []
        with self.history_path.open(encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if site and rec.get("site") != site:
                    continue
                if cutoff and rec.get("at", 0) < cutoff:
                    continue
                out.append(rec)
        return out

    def uptime(self, site: str, window_s: float = 24 * 3600) -> Optional[float]:
        """Fraction of probes in the window that were up. None if no data.

        'login' and 'unknown' probes are excluded from the denominator —
        they say nothing about the site being up.
        """
        recs = self.history(site, since_s=window_s)
        scored = [r for r in recs if r.get("status") in
                  (STATUS_OK, STATUS_DOWN, STATUS_DEGRADED)]
        if not scored:
            return None
        up = sum(1 for r in scored if r["status"] == STATUS_OK)
        return up / len(scored)


class HealthMonitor:
    """Runs tiered probes against registry sites and records results.

    One probe at a time (a lock serializes sweeps): browser automation is
    single-threaded by construction here, same rule as the litellm proxy.
    """

    def __init__(self, store: Optional[HealthStore] = None,
                 profile_dir: Optional[Path] = None,
                 offscreen: bool = True):
        self.store = store or HealthStore()
        self._profile_dir = profile_dir or (_SUBMODULE_ROOT / "data" / ".health_profile")
        self._offscreen = offscreen
        self._probe_lock = threading.Lock()
        self._bundle: Optional[str] = None

    def _bundle_js(self) -> Optional[str]:
        if self._bundle is None:
            path = ext_dir() / "dist" / "uc-extension.js"
            if path.exists():
                self._bundle = path.read_text(encoding="utf-8")
            else:
                logger.warning("UC bundle missing at %s — probes cap at "
                               "'reach' level", path)
                self._bundle = ""
        return self._bundle or None

    # ── probing ─────────────────────────────────────────────────────

    def probe_sites(self, sites: list[SiteEntry]) -> list[ProbeResult]:
        """Probe several sites reusing one browser. Serialized."""
        with self._probe_lock:
            return self._probe_batch(sites)

    def probe_site(self, site: SiteEntry) -> ProbeResult:
        return self.probe_sites([site])[0]

    def _probe_batch(self, sites: list[SiteEntry]) -> list[ProbeResult]:
        from playwright.sync_api import sync_playwright

        results: list[ProbeResult] = []
        args = ["--disable-blink-features=AutomationControlled"]
        if self._offscreen:
            args.append("--window-position=-32000,-32000")
        self._profile_dir.mkdir(parents=True, exist_ok=True)

        with sync_playwright() as p:
            try:
                ctx = p.chromium.launch_persistent_context(
                    str(self._profile_dir), headless=False, args=args,
                    viewport={"width": 1440, "height": 900},
                )
            except Exception as e:
                now = time.time()
                for site in sites:
                    results.append(ProbeResult(
                        site=site.name, status=STATUS_UNKNOWN,
                        level_reached="none", latency_ms=0,
                        detail=f"browser launch failed: {e}"[:200], at=now))
                    self.store.record(results[-1])
                return results

            try:
                from playwright_stealth import Stealth
                Stealth().apply_stealth_sync(ctx)
            except Exception:
                pass

            for site in sites:
                result = self._probe_one(ctx, site)
                self.store.record(result)
                results.append(result)
            ctx.close()
        return results

    def _probe_one(self, ctx, site: SiteEntry) -> ProbeResult:
        started = time.time()

        # Lab intelligence: a fresh site profile redirects the probe to
        # the page where the chat actually lives (homepages usually don't
        # host the composer — measured on the 2026-08 brand-domain sweep).
        profile = None
        try:
            from uc_browser.site_profiles import get_profile_store
            profile = get_profile_store().fresh(site.name)
        except Exception:
            pass
        target_url = profile.chat_page_url if profile else site.url
        via_profile = " [profile]" if profile else ""

        def done(status: str, level: str, detail: str = "") -> ProbeResult:
            return ProbeResult(
                site=site.name, status=status, level_reached=level,
                latency_ms=int((time.time() - started) * 1000),
                detail=(detail + via_profile)[:200], at=started,
            )

        page = ctx.new_page()
        try:
            # ── reach ────────────────────────────────────────────────
            try:
                page.goto(target_url, timeout=30000, wait_until="domcontentloaded")
            except Exception as e:
                return done(STATUS_DOWN, "none", f"goto failed: {e}")
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass

            # Let slow hydrators settle. If a timing profile learned this
            # site's composer-arrival distribution, wait the recommended
            # time (upper-CI + margin) instead of the fixed poll; else
            # poll up to 15s for any visible input.
            learned_wait = profile.detect_wait_ms if profile else None
            if learned_wait:
                page.wait_for_timeout(min(learned_wait, 30000))
            else:
                deadline = time.time() + 15
                while time.time() < deadline:
                    try:
                        if page.evaluate(_READY_INPUT_JS):
                            break
                    except Exception:
                        pass
                    page.wait_for_timeout(1000)

            try:
                shape = page.evaluate(_LOGIN_WALL_JS)
            except Exception as e:
                return done(STATUS_DOWN, "none", f"DOM unreadable: {e}")
            if shape["els"] < 20:
                return done(STATUS_DOWN, "none",
                            f"near-empty DOM ({shape['els']} elements) — "
                            "challenge or failed render")
            if site.probe_level == "reach":
                return done(STATUS_OK, "reach")

            # ── detect ───────────────────────────────────────────────
            bundle = self._bundle_js()
            if not bundle:
                return done(STATUS_OK, "reach", "bundle missing; capped at reach")

            # Profile pre-steps run proactively (the lab already proved
            # they expose the composer here).
            if profile and profile.pre_steps:
                self._run_pre_steps(page, profile.pre_steps)
                page.wait_for_timeout(2000)

            top_score, where = self._detect_across_frames(page, bundle)

            # No composer visible yet? First try the vendor-signature
            # fast path: identify the chat engine by its loader/globals
            # and drive its KNOWN launcher (recipe covers every site on
            # that engine). Fall back to the generic launcher heuristic.
            opened = None
            vendor = None
            if top_score < 4:
                try:
                    from uc_browser import vendor_signatures as vs
                    vendors = vs.identify(page)
                    if vendors:
                        vendor = vendors[0]
                        if vs.open_widget(page, vendor):
                            opened = f"vendor:{vendor}"
                            page.wait_for_timeout(3000)
                            top_score, where = self._detect_across_frames(page, bundle)
                except Exception:
                    pass
            if top_score < 4:
                for _attempt in range(2):
                    clicked = self._try_open_widget(page)
                    if not clicked:
                        break
                    opened = opened or clicked
                    page.wait_for_timeout(2500)
                    top_score, where = self._detect_across_frames(page, bundle)
                    if top_score >= 4:
                        break

            if top_score >= 4:
                via = f" via launcher [{opened}]" if opened else ""
                in_frame = " (iframe)" if where and where != page.main_frame.url else ""
                if site.probe_level == "detect":
                    return done(STATUS_OK, "detect",
                                f"top input score {top_score:.1f}{in_frame}{via}")
                return self._probe_send(page, site, done)

            # No composer. Login wall or genuinely degraded?
            if shape["pw"] or (shape["signalHits"] >= 2 and shape["inputs"] == 0):
                return done(STATUS_LOGIN, "reach",
                            "login wall (password field / sign-in copy, "
                            "no composer)")
            return done(STATUS_DEGRADED, "reach",
                        f"no input cleared the gate (top {top_score:.1f})")
        finally:
            page.close()

    @staticmethod
    def _detect_across_frames(page, bundle: str) -> tuple[float, Optional[str]]:
        """Run the generic input finder in every frame; return the best
        top-1 score and the frame URL it came from. Widget composers
        almost always live inside a cross-origin iframe."""
        best: tuple[float, Optional[str]] = (0.0, None)
        for frame in page.frames:
            try:
                frame.evaluate(bundle)
                inputs = frame.evaluate("window.__UC_findInputs()") or []
            except Exception:
                continue
            score = inputs[0].get("score", 0) if inputs else 0
            if score > best[0]:
                best = (score, frame.url)
        return best

    def _run_pre_steps(self, page, pre_steps: list[dict]) -> None:
        """Execute a profile's declarative pre-steps (best effort)."""
        for step in pre_steps:
            kind = step.get("type")
            try:
                if kind == "launcher-auto":
                    self._try_open_widget(page)
                elif kind == "click" and step.get("selector"):
                    for frame in page.frames:
                        loc = frame.locator(step["selector"]).first
                        if loc.count() > 0:
                            loc.click(timeout=3000)
                            break
            except Exception:
                continue

    @staticmethod
    def _try_open_widget(page) -> Optional[str]:
        """Click the most launcher-looking fixed element (main frame).

        Clicks by viewport coordinates so a launcher that is itself a
        cross-origin iframe still receives the click. Returns a short
        description of what was clicked, or None."""
        try:
            candidates = page.evaluate(_LAUNCHER_CANDIDATES_JS) or []
        except Exception:
            return None
        for cand in candidates[:2]:
            try:
                page.mouse.click(cand["x"], cand["y"])
                return cand["desc"]
            except Exception:
                continue
        return None

    def _probe_send(self, page, site: SiteEntry, done) -> ProbeResult:
        """Level 'send': real round-trip via the site's driver.

        Only Grok has a driver today; a send-level probe costs quota, so
        sites must opt in via probe_level='send'.
        """
        if site.name != "grok":
            return done(STATUS_OK, "detect",
                        "send-level probe unsupported (no driver); "
                        "detect passed")
        try:
            from uc_browser.sites.grok_fast import send_with_fallback
            out = send_with_fallback("Reply with the single word: pong",
                                     wait_for_response=True)
            text = (out or {}).get("response", "")
            if text:
                return done(STATUS_OK, "send", f"round-trip ok ({len(text)} chars)")
            return done(STATUS_DEGRADED, "detect", "send returned no text")
        except Exception as e:
            return done(STATUS_DEGRADED, "detect", f"send failed: {e}")

    # ── scheduling helper ───────────────────────────────────────────

    def due_sites(self, sites: list[SiteEntry]) -> list[SiteEntry]:
        """Sites whose last probe is older than their interval."""
        latest = self.store.latest()
        now = time.time()
        due = []
        for s in sites:
            last = latest.get(s.name, {}).get("at", 0)
            if now - last >= s.probe_interval_s:
                due.append(s)
        return due


def availability_snapshot(registry, store: HealthStore) -> dict:
    """The 'live menu': registry merged with health, shaped for clients.

    A site is *available* when its latest probe is ok AND fresh (younger
    than 2x its probe interval). Sites with a litellm_model additionally
    advertise the model name so API clients know what to request.
    """
    from uc_browser.registry import advertised_model

    now = time.time()
    latest = store.latest()
    sites = []
    for entry in registry.list():
        rec = latest.get(entry.name, {})
        age = now - rec.get("at", 0) if rec else None
        fresh = age is not None and age < 2 * entry.probe_interval_s
        status = rec.get("status", STATUS_UNKNOWN) if fresh else STATUS_UNKNOWN
        model, bootstrapped = advertised_model(entry)
        sites.append({
            "name": entry.name,
            "url": entry.url,
            "kind": entry.kind,
            "status": status,
            "available": status == STATUS_OK,
            "litellm_model": model,
            "bootstrapped": bootstrapped,
            "backup_model": entry.backup_model
                            or os.environ.get("UC_BACKUP_MODEL") or None,
            "callable": bool(model) and status == STATUS_OK,
            "level_reached": rec.get("level_reached") if fresh else None,
            "latency_ms": rec.get("latency_ms") if fresh else None,
            "last_checked": rec.get("at"),
            "checked_age_s": int(age) if age is not None else None,
            "probe_interval_s": entry.probe_interval_s,
            "uptime_24h": store.uptime(entry.name, 24 * 3600),
            "uptime_7d": store.uptime(entry.name, 7 * 24 * 3600),
            "detail": rec.get("detail", ""),
            "login_required": entry.login_required,
        })
    return {
        "generated_at": now,
        "sites": sites,
        "models_available": [s["litellm_model"] for s in sites if s["callable"]],
    }
