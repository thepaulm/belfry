"""Verify the past-playback overlay path end-to-end.

Opens the playback page for cam5 at a recent past timestamp (~5 min
ago — fits inside a recorded window), turns on Show Labels, and
checks that the /api/inference/playback SSE actually fires and
delivers `data:` messages within a reasonable timeout. Not a unit
test, just a regression probe."""

from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta, timezone
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1"


def main() -> int:
    console_errs: list[str] = []
    page_errs: list[str] = []
    sse_data_count = {"n": 0}

    # 5 minutes ago, snap to second resolution. Anything within the
    # last 12 days of footage will work; recent is just nicer for the
    # human watching the run.
    past_ts = (datetime.now(timezone.utc) - timedelta(minutes=5)).replace(microsecond=0)
    past_unix = past_ts.timestamp()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context()
        page = ctx.new_page()

        page.on("pageerror", lambda e: page_errs.append(str(e)))
        page.on(
            "console",
            lambda m: m.type in ("error", "warning") and console_errs.append(
                f"[{m.type}] {m.text}"
            ),
        )
        # Count data: lines flowing through the playback SSE proxy.
        # Playwright doesn't surface SSE message boundaries directly,
        # but it does surface response body sizes in `requestfinished`
        # for closed streams. For an in-flight SSE we instead listen
        # to the body via JS in the page (below).

        url = (
            f"{BASE}/sets/set1/cam5/playback"
            f"?ts={int(past_unix)}"
        )
        print(f"=== goto {url} ===")
        page.goto(url, wait_until="domcontentloaded", timeout=10_000)

        # Wait for the past mp4 to load (player.src set + readyState >= 1).
        try:
            page.wait_for_function(
                "(() => { const v = document.getElementById('player');"
                "  return v && v.src && v.readyState >= 1; })()",
                timeout=10_000,
            )
            print("video src set")
        except Exception as e:
            print(f"video never got a src: {e}")
            return 1

        # Click Show Labels.
        page.click("#labels-toggle")
        print("clicked Show Labels")

        # Pull the past SSE manually from the page so we can count data
        # lines as they arrive (EventSource fires onmessage for each).
        page.evaluate(
            """
            (() => {
                const url = window._lastPastSseUrl || (() => {
                    const candidates = performance.getEntriesByType('resource')
                        .map(e => e.name)
                        .filter(n => n.includes('/api/inference/playback'));
                    return candidates[candidates.length - 1];
                })();
                window._probeMessages = 0;
                if (!url) {
                    window._probeNoUrl = true;
                    return;
                }
                window._probeUrl = url;
                const es = new EventSource(url, { withCredentials: true });
                es.onmessage = () => { window._probeMessages++; };
                window._probeEs = es;
            })();
            """
        )

        # Give it ~12s — server processes a 5-min mp4 in ~9s flat-out.
        deadline = time.monotonic() + 12
        last_n = 0
        while time.monotonic() < deadline:
            page.wait_for_timeout(500)
            n = page.evaluate("window._probeMessages || 0")
            if n != last_n:
                print(f"  past SSE messages received: {n}")
                last_n = n

        sse_data_count["n"] = page.evaluate("window._probeMessages || 0")
        no_url = page.evaluate("!!window._probeNoUrl")

        browser.close()

    print()
    print("=== diagnostics ===")
    print(f"page errors: {len(page_errs)}")
    for e in page_errs[:10]:
        print(f"  {e}")
    print(f"console errors/warnings: {len(console_errs)}")
    for e in console_errs[:10]:
        print(f"  {e}")
    print(f"past SSE messages: {sse_data_count['n']}")
    if no_url:
        print("  (no /api/inference/playback URL ever fired from the page)")

    ok = sse_data_count["n"] > 0 and not page_errs
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
