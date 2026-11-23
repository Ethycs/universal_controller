"""Real CDP lease lifecycle, independent of owner-side Playwright Page caches."""
import json
import os
import threading
from pathlib import Path

import pytest
from playwright.sync_api import Error as PlaywrightError, sync_playwright

from uc_browser.service.leases import LeaseStore
from uc_browser.service.targets import TargetResolver
from uc_browser.service.windows import WindowManager


def test_cdp_revocation_and_sweep_do_not_need_owner_page_objects(tmp_path, monkeypatch):
    with sync_playwright() as pw:
        if not Path(pw.chromium.executable_path).exists():
            pytest.skip("requires playwright install chromium")
        monkeypatch.setenv("UC_SERVICE_HEADLESS", "1")
        config = tmp_path / "windows.json"
        config.write_text(json.dumps({"windows": {"w": {
            "profile": str(tmp_path / "profile"), "engine": "none", "stealth": False,
        }}}))
        manager = WindowManager(config)
        leases = LeaseStore(manager)
        waiting, finished = threading.Event(), threading.Event()
        outcome = []
        thread = None
        try:
            client = leases.register(os.getpid(), "blocked", "w")
            leased = leases.create_page("w", client)
            internal = leases.internal_page("w")
            window = manager.get("w")
            browser = pw.chromium.connect_over_cdp(window.cdp)
            # A page in another context never belongs to the owner's default
            # context.pages, even after event pumping.
            other = browser.new_context()
            raw = other.new_page()
            raw.goto("data:text/html,<p>unleased</p>")
            session = other.new_cdp_session(raw)
            raw_id = session.send("Target.getTargetInfo")["targetInfo"]["targetId"]
            session.detach()
            assert {leased, internal, raw_id} <= {
                p["target_id"] for p in window.info()["pages"]
            }

            def forbid_owner_page_lookup(rt):
                def unavailable(*args, **kwargs):
                    raise AssertionError("lease operations must not resolve owner Page objects")
                rt.scan = rt.resolve = unavailable

            window.owner(forbid_owner_page_lookup).result(5)

            def blocked_client():
                try:
                    with sync_playwright() as client_pw:
                        attached = client_pw.chromium.connect_over_cdp(window.cdp)
                        page = TargetResolver(attached.contexts[0]).resolve(leased)
                        waiting.set()
                        page.wait_for_timeout(600000)
                except BaseException as exc:
                    outcome.append(exc)
                finally:
                    finished.set()

            thread = threading.Thread(target=blocked_client, daemon=True)
            thread.start()
            assert waiting.wait(10)
            assert not finished.wait(0.1)
            assert leases.revoke("blocked") == {"clients": 1, "closed": 1}
            assert finished.wait(5)
            assert len(outcome) == 1 and isinstance(outcome[0], PlaywrightError)
            assert "closed" in str(outcome[0]).lower()

            leases.reap()
            remaining = {p["target_id"]: p for p in window.info()["pages"]}
            assert leased not in remaining and raw_id not in remaining
            assert internal in remaining  # service-owned fetch pages survive
            assert sum(p["url"] == "about:blank" for t, p in remaining.items() if t != internal) == 1
            leases.close_page("w", internal)
            assert internal not in {p["target_id"] for p in window.info()["pages"]}
        finally:
            leases.close()
            manager.close()
            if thread is not None:
                thread.join(5)
                assert not thread.is_alive()
