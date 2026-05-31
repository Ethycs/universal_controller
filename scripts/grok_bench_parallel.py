"""Benchmark: per-session tabs accelerate *switching* between conversations.

We can't get true parallelism from sync Playwright — its greenlet is
thread-bound and crashes if any other thread re-enters. So this is not
a parallel-sends benchmark. What per-session tabs *do* buy is no-cost
switching: when alternating sends between conversation A and B, each
session's tab persists, and we never pay navigation between hops.

Compares:
  1. Shared-tab path (forces every call to default tab, navigating
     between A and B each round-trip).
  2. Per-session-tabs path (each session_key owns a tab, parked on its
     own /c/<uuid> page across hops).

The per-session path should beat the shared path by ~ (navigation cost)
x (number of alternations).
"""
from __future__ import annotations
import time

import litellm

from uc_browser.llm_providers import register_uc_provider
from uc_browser.sites.grok import GrokClient, get_grok_client, reset_grok_singleton


def _send(model: str, prompt: str, *, session_id: str | None = None) -> tuple[float, str, str]:
    t0 = time.perf_counter()
    extras: dict = {"wait_for_response": True}
    if session_id is not None:
        extras["session_id"] = session_id
    resp = litellm.completion(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        extra_body=extras,
        timeout=120,
    )
    return (
        time.perf_counter() - t0,
        resp.choices[0].message.content,
        resp._hidden_params.get("uc_conversation_url", ""),
    )


def main() -> None:
    reset_grok_singleton()
    register_uc_provider()

    # ── Warm up ───────────────────────────────────────────────────────
    print("== warmup ==")
    t0 = time.perf_counter()
    get_grok_client()._ensure_uc()
    print(f"  Playwright launch: {time.perf_counter()-t0:.2f}s")

    # ── Establish two real conversations to alternate between ─────────
    print()
    print("== establish two chats (A, B) — done once, NOT counted ==")
    _, _, url_a = _send("uc/grok", "Reply with one word: alpha", session_id="bench-A")
    _, _, url_b = _send("uc/grok", "Reply with one word: bravo", session_id="bench-B")
    print(f"  chat A: {url_a}")
    print(f"  chat B: {url_b}")

    # ── Per-session-tabs path: alternate A/B/A/B ─────────────────────
    print()
    print("== PER-SESSION-TABS: alternate A->B->A->B (each session parked on own tab) ==")
    per_session_total_t0 = time.perf_counter()
    for sid, prompt in [
        ("bench-A", "Echo back exactly: a1"),
        ("bench-B", "Echo back exactly: b1"),
        ("bench-A", "Echo back exactly: a2"),
        ("bench-B", "Echo back exactly: b2"),
    ]:
        dt, content, _ = _send("uc/grok", prompt, session_id=sid)
        print(f"  [{sid}] {dt:5.2f}s -> {content!r}")
    print(f"  per-session total: {time.perf_counter()-per_session_total_t0:.2f}s")

    # ── Shared-tab path: force every hop through the default tab ─────
    # We simulate it by calling GrokClient directly with no session_key,
    # passing the URL each time so it navigates between A and B.
    print()
    print("== SHARED-TAB: alternate A->B->A->B through DEFAULT tab (each hop navigates) ==")
    gc = get_grok_client()
    shared_total_t0 = time.perf_counter()
    for url, prompt in [
        (url_a, "Echo back exactly: a3"),
        (url_b, "Echo back exactly: b3"),
        (url_a, "Echo back exactly: a4"),
        (url_b, "Echo back exactly: b4"),
    ]:
        t0 = time.perf_counter()
        out = gc.send(prompt, conversation_url=url, timeout_s=120)  # default session
        dt = time.perf_counter() - t0
        print(f"  [{out['conversation_id'][:8]}] {dt:5.2f}s -> {out['response']!r}")
    print(f"  shared total: {time.perf_counter()-shared_total_t0:.2f}s")

    # ── Cleanup ──────────────────────────────────────────────────────
    print()
    print("== cleanup ==")
    for url in (url_a, url_b):
        try:
            ok = gc.delete(url)
            print(f"  delete {url[-12:]}: {ok}")
        except Exception as e:
            print(f"  delete {url[-12:]}: error {e}")


if __name__ == "__main__":
    main()
