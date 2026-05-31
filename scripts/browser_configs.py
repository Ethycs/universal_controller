"""Browser configuration presets for the test harness.

Each BrowserConfig has a stable BC-NNN ID matching the entry in
docs/browser_attempts.md. The harness in browser_test.py reads from
the CONFIGS dict to launch and validate a configuration.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

_REPO = Path(__file__).resolve().parent.parent
_DATA = _REPO / "data"
_LOCALAPPDATA = Path(os.environ.get("LOCALAPPDATA", ""))


def _playwright_chromium_path() -> str:
    """Locate Playwright's bundled Chromium binary."""
    pw_dir = _LOCALAPPDATA / "ms-playwright"
    matches = sorted(pw_dir.glob("chromium-*/chrome-win64/chrome.exe"))
    if matches:
        return str(matches[-1])
    return ""


def _chrome_path() -> str:
    """Locate installed Chrome binary."""
    for candidate in [
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
        Path.home() / r"AppData\Local\Google\Chrome\Application\chrome.exe",
    ]:
        if candidate.exists():
            return str(candidate)
    return ""


def _real_chrome_profile() -> str:
    """Locate the user's real Chrome User Data directory."""
    p = _LOCALAPPDATA / "Google" / "Chrome" / "User Data"
    return str(p) if p.exists() else ""


def _keepassxc_extension_path() -> str:
    """Locate the KeePassXC-Browser extension on disk."""
    ext_dir = _LOCALAPPDATA / "Google/Chrome/User Data/Default/Extensions/oboonakemofpalcgghocfoadofidjkkk"
    if not ext_dir.exists():
        return ""
    versions = sorted(ext_dir.iterdir())
    if not versions:
        return ""
    # Use a clean copy without _metadata to avoid load-time errors
    clean = _DATA / ".keepassxc_extension"
    if not clean.exists():
        import shutil
        clean.mkdir(parents=True, exist_ok=True)
        for item in versions[-1].iterdir():
            if item.name == "_metadata":
                continue
            dst = clean / item.name
            if item.is_dir():
                if not dst.exists():
                    shutil.copytree(str(item), str(dst))
            else:
                shutil.copy2(str(item), str(dst))
    return str(clean)


@dataclass
class BrowserConfig:
    """Parameterized description of a browser launch attempt."""

    id: str
    description: str

    binary: Literal["chrome", "chromium", "playwright_chromium"]
    profile_strategy: Literal["empty", "real", "copy_auth", "copy_full", "playwright_dir"]
    profile_path: str | None
    launch_method: Literal["playwright_persistent", "subprocess_cdp"]

    cdp_port: int | None = None
    extension_path: str | None = None
    args: list[str] = field(default_factory=list)
    ignore_default_args: list[str] = field(default_factory=list)
    register_native_messaging: bool = False

    # Post-launch hooks
    inject_state_file: str | None = None  # path to playwright storage_state JSON to inject as cookies

    # auth_test phase — pick a URL the fixture has cookies for
    auth_test_url: str = "https://lu.ma/signin"
    # Substrings indicating a logged-in state in the page HTML
    auth_test_signals: tuple[str, ...] = ("My Calendar", "Account", "Profile")

    # Phases this config is expected to fail at; if it gets further, that's news.
    expected_fail_phase: str | None = None
    expected_outcome: str | None = None  # human description

    def resolve_binary(self) -> str:
        if self.binary == "chrome":
            return _chrome_path()
        if self.binary == "playwright_chromium":
            return _playwright_chromium_path()
        # "chromium" — Playwright's bundled is the only one available locally
        return _playwright_chromium_path()

    def resolve_profile_path(self) -> str:
        if self.profile_path:
            return self.profile_path
        if self.profile_strategy == "real":
            return _real_chrome_profile()
        if self.profile_strategy == "playwright_dir":
            return str(_DATA / f".chrome_profile_{self.id.lower()}")
        return str(_DATA / f".test_profile_{self.id.lower()}")


# ─── Presets ──────────────────────────────────────────────────────────

CONFIGS: dict[str, BrowserConfig] = {
    "BC-001": BrowserConfig(
        id="BC-001",
        description="Playwright launch_persistent_context + chrome channel + isolated profile",
        binary="chrome",
        profile_strategy="playwright_dir",
        profile_path=str(_DATA / ".chrome_profile"),
        launch_method="playwright_persistent",
        args=["--disable-blink-features=AutomationControlled"],
        expected_fail_phase=None,  # works for manual login
        expected_outcome="WORKS for manual login + session persistence; no Google password sync",
    ),
    "BC-002": BrowserConfig(
        id="BC-002",
        description="subprocess Chrome + real User Data profile + CDP port",
        binary="chrome",
        profile_strategy="real",
        profile_path=None,
        launch_method="subprocess_cdp",
        cdp_port=9222,
        expected_fail_phase="launch",
        expected_outcome="FAIL: 'DevTools remote debugging requires a non-default data directory.'",
    ),
    "BC-003": BrowserConfig(
        id="BC-003",
        description="subprocess Chrome + minimal auth-file copy",
        binary="chrome",
        profile_strategy="copy_auth",
        profile_path=str(_DATA / ".native_chrome_profile_minimal"),
        launch_method="subprocess_cdp",
        cdp_port=9222,
        expected_fail_phase="launch",
        expected_outcome="FAIL: Chrome crashes — incomplete profile state",
    ),
    "BC-004": BrowserConfig(
        id="BC-004",
        description="subprocess Chrome + full Default copy minus caches",
        binary="chrome",
        profile_strategy="copy_full",
        profile_path=str(_DATA / ".native_chrome_profile_full"),
        launch_method="subprocess_cdp",
        cdp_port=9222,
        expected_fail_phase="launch",
        expected_outcome="FAIL: Chrome crashes — SQLite journals/locks",
    ),
    "BC-005": BrowserConfig(
        id="BC-005",
        description="Playwright + real User Data + --profile-directory=EventHarvester",
        binary="chrome",
        profile_strategy="real",
        profile_path=None,
        launch_method="playwright_persistent",
        args=[
            "--disable-blink-features=AutomationControlled",
            "--profile-directory=EventHarvester",
        ],
        expected_fail_phase="launch",
        expected_outcome="FAIL: exit 21 — Playwright args corrupt real profile",
    ),
    "BC-006": BrowserConfig(
        id="BC-006",
        description="rookiepy cookie injection into Playwright Chromium (no extension)",
        binary="playwright_chromium",
        profile_strategy="playwright_dir",
        profile_path=str(_DATA / ".uc_chromium_profile"),
        launch_method="playwright_persistent",
        args=["--disable-blink-features=AutomationControlled"],
        expected_fail_phase="auth_test",
        expected_outcome="FAIL: rookiepy returns 0 cookies (encryption + DB lock)",
    ),
    "BC-007": BrowserConfig(
        id="BC-007",
        description="Playwright Chromium + storage_state cookies from web_login",
        binary="playwright_chromium",
        profile_strategy="playwright_dir",
        profile_path=str(_DATA / ".uc_chromium_profile"),
        launch_method="playwright_persistent",
        args=["--disable-blink-features=AutomationControlled"],
        inject_state_file=str(_DATA / ".playwright_state.json"),
        # Test against eventbrite (52 cookies) — user has session there
        auth_test_url="https://www.eventbrite.com/account-settings/",
        auth_test_signals=("Account", "Sign out", "Log out", "Settings"),
        expected_fail_phase=None,
        expected_outcome="WORKS for sessions saved during web_login (current production path)",
    ),
    "BC-008": BrowserConfig(
        id="BC-008",
        description="Playwright + chrome channel + KeePassXC extension",
        binary="chrome",
        profile_strategy="playwright_dir",
        profile_path=str(_DATA / ".chrome_profile_keepassxc"),
        launch_method="playwright_persistent",
        extension_path=_keepassxc_extension_path(),
        args=["--disable-blink-features=AutomationControlled"],
        expected_fail_phase="launch",
        expected_outcome="FAIL: exit 21 — branded Chrome blocks --load-extension",
    ),
    "BC-009": BrowserConfig(
        id="BC-009",
        description="Playwright Chromium + KeePassXC extension + ignore --disable-extensions",
        binary="playwright_chromium",
        profile_strategy="playwright_dir",
        profile_path=str(_DATA / ".keepassxc_test_profile"),
        launch_method="playwright_persistent",
        extension_path=_keepassxc_extension_path(),
        ignore_default_args=["--disable-extensions"],
        expected_fail_phase="launch",
        expected_outcome="FAIL: 0x80000003 crash — pipe CDP conflicts with --load-extension",
    ),
    "BC-010": BrowserConfig(
        id="BC-010",
        description="subprocess Playwright Chromium + CDP port + KeePassXC extension",
        binary="playwright_chromium",
        profile_strategy="playwright_dir",
        profile_path=str(_DATA / ".keepassxc_test_profile"),
        launch_method="subprocess_cdp",
        cdp_port=9223,
        extension_path=_keepassxc_extension_path(),
        args=[
            "--no-first-run",
            "--no-default-browser-check",
            "--no-sandbox",
        ],
        register_native_messaging=True,
        # Probe a site that has a login form so we can later detect auto-fill.
        # auth_test signal: KeePassXC extension content script injects an icon
        # into username fields when it detects credentials.
        auth_test_url="https://www.eventbrite.com/signin/",
        auth_test_signals=("Sign in", "Log in", "Email"),  # form is reachable
        expected_fail_phase=None,
        expected_outcome="WORKS for extension load; auto-fill detection requires KeePassXC unlocked + entries",
    ),
    "BC-011": BrowserConfig(
        id="BC-011",
        description="BC-010 minus --no-sandbox (try to defeat Google auth block)",
        binary="playwright_chromium",
        profile_strategy="playwright_dir",
        profile_path=str(_DATA / ".keepassxc_test_profile_011"),
        launch_method="subprocess_cdp",
        cdp_port=9224,
        extension_path=_keepassxc_extension_path(),
        args=[
            "--no-first-run",
            "--no-default-browser-check",
            # No --no-sandbox; Google checks for it
        ],
        register_native_messaging=True,
        auth_test_url="https://lu.ma/signin",
        auth_test_signals=("Sign in", "Log in", "Email"),
        expected_fail_phase=None,
        expected_outcome="testing whether removing --no-sandbox lets Google auth render",
    ),
}


def list_configs() -> list[BrowserConfig]:
    return [CONFIGS[k] for k in sorted(CONFIGS.keys())]


def get_config(config_id: str) -> BrowserConfig | None:
    return CONFIGS.get(config_id)


def next_config_id() -> str:
    """Return the next available BC-NNN ID."""
    nums = []
    for cid in CONFIGS:
        if cid.startswith("BC-"):
            try:
                nums.append(int(cid.split("-")[1]))
            except ValueError:
                continue
    next_n = (max(nums) + 1) if nums else 1
    return f"BC-{next_n:03d}"
