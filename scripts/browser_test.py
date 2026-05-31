"""Browser configuration test harness.

Reads BrowserConfig presets from browser_configs.py, launches each
configuration, runs a series of phase checks, and appends the result
to data/browser_test_results.jsonl.

Usage:
    pixi run python scripts/browser_test.py --list
    pixi run python scripts/browser_test.py --run BC-010
    pixi run python scripts/browser_test.py --run BC-007,BC-010
    pixi run python scripts/browser_test.py --report
    pixi run python scripts/browser_test.py --new "describe a new attempt"
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from browser_configs import (  # noqa: E402
    BrowserConfig,
    get_config,
    list_configs,
    next_config_id,
)

_REPO = Path(__file__).resolve().parent.parent
_DATA = _REPO / "data"
_RESULTS = _DATA / "browser_test_results.jsonl"


@dataclass
class PhaseResult:
    name: str
    status: str  # PASS / FAIL / SKIP
    detail: str = ""


@dataclass
class TestResult:
    id: str
    date: str
    phases: list[PhaseResult] = field(default_factory=list)
    duration_s: float = 0.0
    notes: str = ""

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "date": self.date,
            "phases": {p.name: f"{p.status}: {p.detail}" if p.detail else p.status for p in self.phases},
            "duration_s": round(self.duration_s, 2),
            "notes": self.notes,
        }


# ─── Phase helpers ────────────────────────────────────────────────────


def _phase_pass(name: str, detail: str = "") -> PhaseResult:
    return PhaseResult(name, "PASS", detail)


def _phase_fail(name: str, detail: str) -> PhaseResult:
    return PhaseResult(name, "FAIL", detail)


def _phase_skip(name: str, detail: str = "") -> PhaseResult:
    return PhaseResult(name, "SKIP", detail)


# ─── Profile preparation ──────────────────────────────────────────────


def _prepare_profile(cfg: BrowserConfig) -> tuple[str | None, str]:
    """Set up the profile directory according to cfg.profile_strategy.

    Returns (profile_path, message). If profile_path is None, the
    config's profile is unusable.
    """
    if cfg.profile_strategy == "real":
        path = cfg.resolve_profile_path()
        if not path:
            return None, "real Chrome profile not found"
        return path, f"using real profile: {path}"

    if cfg.profile_strategy == "empty":
        path = cfg.resolve_profile_path()
        Path(path).mkdir(parents=True, exist_ok=True)
        return path, f"empty profile: {path}"

    if cfg.profile_strategy == "playwright_dir":
        path = cfg.resolve_profile_path()
        Path(path).mkdir(parents=True, exist_ok=True)
        return path, f"playwright-managed profile: {path}"

    if cfg.profile_strategy in ("copy_auth", "copy_full"):
        from uc_browser.browser import (  # type: ignore
            _copy_chrome_auth,
            _find_real_chrome_profile,
        )

        src = _find_real_chrome_profile()
        if not src:
            return None, "real Chrome profile not found for copy"
        dst_path = cfg.resolve_profile_path()
        dst = Path(dst_path)
        if dst.exists():
            shutil.rmtree(str(dst), ignore_errors=True)
        _copy_chrome_auth(src, dst)
        return str(dst), f"copied profile to: {dst_path}"

    return None, f"unknown profile_strategy: {cfg.profile_strategy}"


# ─── Native messaging ─────────────────────────────────────────────────


def _register_chromium_native_messaging() -> str:
    """Register KeePassXC native messaging host under Chromium key."""
    if sys.platform != "win32":
        return "skipped (not Windows)"
    import winreg

    manifest = (
        Path(os.environ["LOCALAPPDATA"])
        / "KeePassXC/org.keepassxc.keepassxc_browser_chromium.json"
    )
    if not manifest.exists():
        return f"manifest not found: {manifest}"

    key_path = r"Software\Chromium\NativeMessagingHosts\org.keepassxc.keepassxc_browser"
    try:
        key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path)
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, str(manifest))
        winreg.CloseKey(key)
        return f"registered -> {manifest.name}"
    except Exception as e:
        return f"failed: {e}"


# ─── Launch + connect ─────────────────────────────────────────────────


def _build_launch_args(cfg: BrowserConfig, profile_path: str) -> list[str]:
    args = list(cfg.args)
    if cfg.extension_path:
        args.append(f"--load-extension={cfg.extension_path}")
        args.append(f"--disable-extensions-except={cfg.extension_path}")
    if cfg.launch_method == "subprocess_cdp" and cfg.cdp_port:
        args.append(f"--remote-debugging-port={cfg.cdp_port}")
        args.append(f"--user-data-dir={profile_path}")
    return args


def _launch_subprocess(cfg: BrowserConfig, profile_path: str) -> tuple[subprocess.Popen | None, str]:
    binary = cfg.resolve_binary()
    if not binary:
        return None, f"binary not found: {cfg.binary}"

    args = _build_launch_args(cfg, profile_path)
    cmd = [binary] + args + ["about:blank"]
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    time.sleep(3)
    if proc.poll() is not None:
        try:
            _, stderr = proc.communicate(timeout=2)
            err = stderr.decode("utf-8", errors="replace")[:300]
        except Exception:
            err = "no stderr captured"
        return None, f"exited code={proc.returncode}: {err}"
    return proc, f"pid={proc.pid}"


def _launch_playwright_persistent(cfg: BrowserConfig, profile_path: str):
    """Returns (context, error_message). On success, context is returned."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        return None, None, f"playwright not installed: {e}"

    pw = sync_playwright().start()
    binary_kwargs = {}
    if cfg.binary == "chrome":
        binary_kwargs["channel"] = "chrome"
    elif cfg.binary == "playwright_chromium":
        binary_kwargs["channel"] = None

    args = _build_launch_args(cfg, profile_path)
    try:
        context = pw.chromium.launch_persistent_context(
            profile_path,
            headless=False,
            args=args,
            ignore_default_args=cfg.ignore_default_args or None,
            **binary_kwargs,
        )
        return pw, context, ""
    except Exception as e:
        try:
            pw.stop()
        except Exception:
            pass
        return None, None, f"{type(e).__name__}: {str(e)[:300]}"


# ─── Phase implementations ────────────────────────────────────────────


def _run_phases(cfg: BrowserConfig) -> tuple[list[PhaseResult], str]:
    phases: list[PhaseResult] = []
    notes: list[str] = []

    profile_path, prof_msg = _prepare_profile(cfg)
    notes.append(prof_msg)
    if profile_path is None:
        phases.append(_phase_fail("launch", prof_msg))
        return phases, " | ".join(notes)

    if cfg.register_native_messaging:
        nm_msg = _register_chromium_native_messaging()
        notes.append(f"native_messaging: {nm_msg}")

    proc = None
    pw = None
    context = None
    browser = None

    try:
        # Phase 1: launch
        if cfg.launch_method == "subprocess_cdp":
            proc, msg = _launch_subprocess(cfg, profile_path)
            if proc is None:
                phases.append(_phase_fail("launch", msg))
                return phases, " | ".join(notes)
            phases.append(_phase_pass("launch", msg))

            # Phase 2: connect
            from playwright.sync_api import sync_playwright

            pw = sync_playwright().start()
            for _ in range(15):
                time.sleep(1)
                try:
                    browser = pw.chromium.connect_over_cdp(f"http://localhost:{cfg.cdp_port}")
                    break
                except Exception:
                    continue
            if not browser:
                phases.append(_phase_fail("connect", f"no CDP response on port {cfg.cdp_port}"))
                return phases, " | ".join(notes)
            phases.append(_phase_pass("connect", f"CDP port {cfg.cdp_port}"))
            context = browser.contexts[0] if browser.contexts else browser.new_context()

        else:  # playwright_persistent
            pw, context, err = _launch_playwright_persistent(cfg, profile_path)
            if context is None:
                phases.append(_phase_fail("launch", err))
                return phases, " | ".join(notes)
            phases.append(_phase_pass("launch", "persistent context"))
            phases.append(_phase_pass("connect", "via launch_persistent_context"))

        # Inject cookies from state file if configured
        if cfg.inject_state_file:
            sf = Path(cfg.inject_state_file)
            if sf.exists():
                try:
                    state = json.loads(sf.read_text(encoding="utf-8"))
                    cookies = state.get("cookies", [])
                    if cookies:
                        context.add_cookies(cookies)
                        notes.append(f"injected {len(cookies)} cookies from {sf.name}")
                except Exception as e:
                    notes.append(f"cookie injection failed: {e}")
            else:
                notes.append(f"state file missing: {sf}")

        # Phase 3: extension_loads
        if cfg.extension_path:
            sw = None
            if context.service_workers:
                sw = context.service_workers[0]
                phases.append(_phase_pass("extension_loads", f"sw: {sw.url[:60]}"))
            else:
                try:
                    sw = context.wait_for_event("serviceworker", timeout=20000)
                    phases.append(_phase_pass("extension_loads", f"sw: {sw.url[:60]}"))
                except Exception as e:
                    bgs = context.background_pages
                    if bgs:
                        phases.append(_phase_pass("extension_loads", f"bg page: {bgs[0].url[:60]}"))
                    else:
                        phases.append(_phase_fail("extension_loads", f"no SW or BG page: {e}"))
        else:
            phases.append(_phase_skip("extension_loads", "no extension configured"))

        # Phase 4: page_loads
        try:
            page = context.new_page()
            resp = page.goto("https://example.com", timeout=15000, wait_until="domcontentloaded")
            status = resp.status if resp else 0
            if status == 200:
                phases.append(_phase_pass("page_loads", f"status={status}"))
            else:
                phases.append(_phase_fail("page_loads", f"status={status}"))
            page.close()
        except Exception as e:
            phases.append(_phase_fail("page_loads", f"{type(e).__name__}: {str(e)[:120]}"))

        # Phase 5: auth_test (best-effort — manual inspection still required)
        auth_page = None
        try:
            auth_page = context.new_page()
            auth_page.goto(cfg.auth_test_url, timeout=20000, wait_until="domcontentloaded")
            time.sleep(4)  # let extension content scripts inject
            html = auth_page.content()
            has_signal = any(s.lower() in html.lower() for s in cfg.auth_test_signals)
            if has_signal:
                matched = next(s for s in cfg.auth_test_signals if s.lower() in html.lower())
                phases.append(_phase_pass("auth_test", f"signal: '{matched}'"))
            else:
                phases.append(_phase_fail("auth_test", f"no auth signal at {cfg.auth_test_url}"))
        except Exception as e:
            phases.append(_phase_fail("auth_test", f"{type(e).__name__}: {str(e)[:120]}"))

        # Phase 5b: keepassxc_visible — only when KeePassXC extension is loaded
        if cfg.extension_path and "keepassxc" in str(cfg.extension_path).lower():
            try:
                if auth_page is None:
                    raise RuntimeError("auth_page not available")
                kpxc = auth_page.evaluate(
                    """() => ({
                        cls: document.querySelectorAll('[class*=\"kpxc\"]').length,
                        id: document.querySelectorAll('[id*=\"kpxc\"]').length,
                        attrs: Array.from(document.querySelectorAll('*'))
                            .reduce((a, el) => a + el.getAttributeNames()
                                .filter(n => n.startsWith('data-kpxc')).length, 0),
                    })"""
                )
                total = kpxc["cls"] + kpxc["id"] + kpxc["attrs"]
                if total > 0:
                    phases.append(_phase_pass(
                        "keepassxc_visible",
                        f"cls={kpxc['cls']} id={kpxc['id']} attrs={kpxc['attrs']}",
                    ))
                else:
                    phases.append(_phase_fail(
                        "keepassxc_visible",
                        "no kpxc-* markers (KeePassXC may be locked)",
                    ))
            except Exception as e:
                phases.append(_phase_fail("keepassxc_visible", f"{type(e).__name__}: {e}"))

        if auth_page:
            try:
                auth_page.close()
            except Exception:
                pass

        # Phase 6: shutdown
        try:
            if context:
                context.close()
            if browser:
                browser.close()
            if pw:
                pw.stop()
            if proc:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
            phases.append(_phase_pass("shutdown", "clean"))
        except Exception as e:
            phases.append(_phase_fail("shutdown", f"{type(e).__name__}: {str(e)[:120]}"))

    finally:
        # Best-effort cleanup
        try:
            if proc and proc.poll() is None:
                proc.kill()
        except Exception:
            pass

    return phases, " | ".join(notes)


# ─── Result persistence ───────────────────────────────────────────────


def _record_result(result: TestResult) -> None:
    _RESULTS.parent.mkdir(parents=True, exist_ok=True)
    with open(_RESULTS, "a", encoding="utf-8") as f:
        f.write(json.dumps(result.to_json()) + "\n")


def _load_results() -> list[dict]:
    if not _RESULTS.exists():
        return []
    out = []
    for line in _RESULTS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _last_result_for(config_id: str) -> dict | None:
    rows = [r for r in _load_results() if r.get("id") == config_id]
    return rows[-1] if rows else None


# ─── CLI commands ─────────────────────────────────────────────────────


def cmd_list() -> None:
    print(f"{'ID':<8} {'Last status':<14} {'Description'}")
    print("-" * 100)
    for cfg in list_configs():
        last = _last_result_for(cfg.id)
        if last:
            phases = last.get("phases", {})
            failed = [k for k, v in phases.items() if v.startswith("FAIL")]
            if failed:
                status = f"FAIL@{failed[0]}"
            else:
                status = "PASS"
            status = f"{status} ({last.get('date', '')[:10]})"
        else:
            status = "(not run)"
        print(f"{cfg.id:<8} {status:<14} {cfg.description[:80]}")


def cmd_run(config_ids: list[str]) -> int:
    rc = 0
    for cid in config_ids:
        cfg = get_config(cid)
        if not cfg:
            print(f"Unknown config: {cid}")
            rc = 1
            continue
        print(f"\n=== Running {cfg.id}: {cfg.description} ===")
        if cfg.expected_outcome:
            print(f"Expected: {cfg.expected_outcome}")
        print()

        t0 = time.time()
        phases, notes = _run_phases(cfg)
        duration = time.time() - t0

        result = TestResult(
            id=cfg.id,
            date=datetime.now(timezone.utc).isoformat(),
            phases=phases,
            duration_s=duration,
            notes=notes,
        )
        _record_result(result)

        # Print phase summary
        for p in phases:
            mark = {"PASS": "[OK]", "FAIL": "[FAIL]", "SKIP": "[SKIP]"}.get(p.status, "[?]")
            print(f"  {mark} {p.name}: {p.detail}")
        print(f"\n  Duration: {duration:.1f}s")
        print(f"  Recorded -> {_RESULTS}")

        if any(p.status == "FAIL" for p in phases):
            rc = 1
    return rc


def cmd_report() -> None:
    rows = _load_results()
    if not rows:
        print("No results recorded yet.")
        return
    # Take latest per config
    latest: dict[str, dict] = {}
    for r in rows:
        latest[r["id"]] = r

    phase_names = [
        "launch", "connect", "extension_loads", "page_loads",
        "auth_test", "keepassxc_visible", "shutdown",
    ]
    header = f"{'ID':<8}" + "".join(f" {n[:8]:<10}" for n in phase_names) + " duration"
    print(header)
    print("-" * len(header))
    for cid in sorted(latest.keys()):
        r = latest[cid]
        cells = []
        for n in phase_names:
            val = r.get("phases", {}).get(n, "—")
            cells.append(val.split(":")[0][:9])
        print(f"{cid:<8}" + "".join(f" {c:<10}" for c in cells) + f" {r.get('duration_s', 0):.1f}s")


def cmd_new(description: str) -> None:
    cid = next_config_id()
    print(f"Next config ID: {cid}")
    print()
    print("Add this stub to scripts/browser_configs.py CONFIGS dict:")
    print()
    print(f'    "{cid}": BrowserConfig(')
    print(f'        id="{cid}",')
    print(f'        description="{description}",')
    print('        binary="playwright_chromium",  # TODO')
    print('        profile_strategy="playwright_dir",  # TODO')
    print('        profile_path=None,')
    print('        launch_method="subprocess_cdp",  # TODO')
    print('        cdp_port=9224,  # TODO')
    print('        extension_path=None,')
    print('        args=[],')
    print('        ignore_default_args=[],')
    print('        register_native_messaging=False,')
    print('        expected_fail_phase=None,')
    print('        expected_outcome=None,')
    print('    ),')
    print()
    print(f"Then add an entry to docs/browser_attempts.md as {cid}.")


def main():
    p = argparse.ArgumentParser(description="Browser configuration test harness")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--list", action="store_true", help="List all configs with last outcome")
    g.add_argument("--run", metavar="ID[,ID,...]", help="Run one or more configs")
    g.add_argument("--report", action="store_true", help="Show phase grid summary")
    g.add_argument("--new", metavar="DESC", help="Scaffold next BC-NNN config")
    args = p.parse_args()

    if args.list:
        cmd_list()
    elif args.run:
        ids = [s.strip() for s in args.run.split(",") if s.strip()]
        sys.exit(cmd_run(ids))
    elif args.report:
        cmd_report()
    elif args.new:
        cmd_new(args.new)


if __name__ == "__main__":
    main()
