"""Benchmark GrokClient: cold start, warm send (with + without wait), delete.

Prints per-phase wall times so we can pinpoint the slow steps.
"""
from __future__ import annotations
import os
import time
from contextlib import contextmanager

from uc_browser.sites.grok import reset_grok_singleton, get_grok_client


@contextmanager
def timed(label: str):
    t0 = time.perf_counter()
    try:
        yield
    finally:
        dt = time.perf_counter() - t0
        print(f"  {label:40s} {dt:6.2f} s")


def run():
    reset_grok_singleton()
    gc = get_grok_client()

    print("== COLD START ==")
    with timed("first _ensure_uc (Playwright launch)"):
        gc._ensure_uc()

    print("\n== WARM SEND #1 (full response wait) ==")
    t0 = time.perf_counter()
    with timed("send('reply: alpha', timeout=120)"):
        out1 = gc.send("Reply with exactly one word: alpha", timeout_s=120)
    print(f"    response: {out1['response']!r}")
    print(f"    url:      {out1['url']}")
    print(f"    conv_id:  {out1['conversation_id']}")
    print(f"  TOTAL #1: {time.perf_counter()-t0:.2f} s")
    url = out1["url"]

    print("\n== WARM SEND #2 (same chat, full response wait) ==")
    t0 = time.perf_counter()
    with timed("send('reply: beta', conversation_url=...)"):
        out2 = gc.send(
            "Reply with exactly one word: beta",
            conversation_url=url,
            timeout_s=120,
        )
    print(f"    response: {out2['response']!r}")
    print(f"  TOTAL #2: {time.perf_counter()-t0:.2f} s")

    # If the optimized path exposes wait_for_response, exercise it too.
    if "wait_for_response" in gc.send.__code__.co_varnames:
        print("\n== BLIND SEND #3 (wait_for_response=False) ==")
        t0 = time.perf_counter()
        with timed("send(blind)"):
            out3 = gc.send(
                "Reply with exactly one word: gamma",
                conversation_url=url,
                wait_for_response=False,
            )
        print(f"    response: {out3['response']!r}")
        print(f"  TOTAL #3: {time.perf_counter()-t0:.2f} s")

    print("\n== LIST + DELETE (cleanup the alpha chat) ==")
    t0 = time.perf_counter()
    with timed("list_conversations()"):
        items = gc.list_conversations()
    print(f"    sidebar count: {len(items)}")

    t0 = time.perf_counter()
    with timed("delete(url)"):
        ok = gc.delete(url)
    print(f"    delete -> {ok}")
    print(f"  TOTAL delete: {time.perf_counter()-t0:.2f} s")

    gc.close()


if __name__ == "__main__":
    run()
