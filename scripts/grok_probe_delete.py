"""One-shot probe: identify Grok's sidebar Options menu + confirm dialog DOM."""
import json
from uc_browser import BrowserMode, UCBrowser

TARGET_ID = "c14a1ddb-1d5b-4561-bb18-01ff581f585c"

uc = UCBrowser(mode=BrowserMode.CHROMIUM_EXT, timeout_ms=30000)
uc.start()
try:
    page = uc.open("https://grok.com/", wait_ms=5000)
    uc.dismiss_cookies(page)
    uc.close_modal(page)
    page.wait_for_timeout(2000)

    print("=== locate the row by href + dump its options button ===")
    info = page.evaluate(
        """(id) => {
            const a = document.querySelector('a[href="/c/' + id + '"]');
            if (!a) return {error: 'row not found'};
            const li = a.closest('[data-sidebar="menu-item"]');
            if (!li) return {error: 'no enclosing menu-item'};
            const opts = li.querySelector('button[aria-label="Options" i]');
            const r = opts?.getBoundingClientRect();
            return {
                li_state: li.firstElementChild?.getAttribute('data-state'),
                opts_visible: r ? (r.width > 0 && r.height > 0) : false,
                opts_rect: r ? {w: Math.round(r.width), h: Math.round(r.height)} : null,
            };
        }""",
        TARGET_ID,
    )
    print(json.dumps(info, indent=2))

    print()
    print("=== playwright hover the link + dump options state ===")
    a_loc = page.locator('a[href="/c/' + TARGET_ID + '"]').first
    a_loc.scroll_into_view_if_needed()
    a_loc.hover()
    page.wait_for_timeout(500)
    after = page.evaluate(
        """(id) => {
            const a = document.querySelector('a[href="/c/' + id + '"]');
            const li = a.closest('[data-sidebar="menu-item"]');
            const opts = li.querySelector('button[aria-label="Options" i]');
            const r = opts?.getBoundingClientRect();
            return {
                li_state: li.firstElementChild?.getAttribute('data-state'),
                opts_visible: r ? (r.width > 0 && r.height > 0) : false,
                opts_rect: r ? {w: Math.round(r.width), h: Math.round(r.height), top: Math.round(r.top), left: Math.round(r.left)} : null,
            };
        }""",
        TARGET_ID,
    )
    print(json.dumps(after, indent=2))

    print()
    print("=== force-click Options + dump popover ===")
    li_loc = page.locator('a[href="/c/' + TARGET_ID + '"]').locator("xpath=ancestor::li[1]")
    opts_loc = li_loc.locator('button[aria-label="Options" i]')
    opts_loc.click(force=True)
    page.wait_for_timeout(800)
    menu_items = page.evaluate(
        """() => {
            const candidates = document.querySelectorAll('[role="menuitem"]');
            const out = [];
            candidates.forEach(el => {
                const r = el.getBoundingClientRect();
                out.push({
                    text: (el.innerText || '').trim().slice(0, 60),
                    role: el.getAttribute('role'),
                    data_testid: el.getAttribute('data-testid'),
                    visible: r.width > 0 && r.height > 0,
                    tag: el.tagName.toLowerCase(),
                });
            });
            return out;
        }"""
    )
    print(json.dumps(menu_items, indent=2))

    print()
    print("=== click Delete item + dump confirm dialog ===")
    clicked = page.evaluate(
        """() => {
            for (const el of document.querySelectorAll('[role="menuitem"]')) {
                const t = (el.innerText || '').trim().toLowerCase();
                if (t === 'delete' || t.startsWith('delete')) {
                    el.click();
                    return {ok: true, text: t};
                }
            }
            return {ok: false};
        }"""
    )
    print("click delete:", clicked)
    page.wait_for_timeout(800)
    dialog = page.evaluate(
        """() => {
            const dlgs = document.querySelectorAll('[role="alertdialog"], [role="dialog"]');
            const out = [];
            dlgs.forEach(d => {
                const r = d.getBoundingClientRect();
                if (r.width <= 0 || r.height <= 0) return;
                const buttons = Array.from(d.querySelectorAll('button')).map(b => ({
                    text: (b.innerText || '').trim().slice(0, 30),
                    aria_label: b.getAttribute('aria-label'),
                    data_testid: b.getAttribute('data-testid'),
                    cls: (b.className || '').toString().slice(0, 100),
                }));
                out.push({
                    role: d.getAttribute('role'),
                    text: (d.innerText || '').trim().slice(0, 200),
                    buttons: buttons,
                });
            });
            return out;
        }"""
    )
    print(json.dumps(dialog, indent=2))
finally:
    page.close()
    uc.close()
