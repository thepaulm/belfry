"""Reproduce the set1 → set2 → set1 with labels-on bug.

Captures console errors, page errors, and the result of every network
request issued during the third navigation. Prints a diagnostic
summary at the end. Not committed as a test (no harness); just a
debugging probe."""

from __future__ import annotations

import sys
import time
from collections import Counter
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1"


def main() -> int:
    console_errs: list[str] = []
    page_errs: list[str] = []
    failed_reqs: list[tuple[str, str]] = []   # (method+url, failure_text)
    pending_at_end: list[str] = []
    request_log: list[tuple[float, str, str]] = []  # (start, method, url)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context()

        # No localStorage pre-seed any more — each session starts
        # labels-OFF; we click the toggle on the first page below.

        page = ctx.new_page()
        page.on(
            "console",
            lambda m: m.type in ("error", "warning") and console_errs.append(
                f"[{m.type}] {m.text}"
            ),
        )
        page.on("pageerror", lambda e: page_errs.append(str(e)))
        page.on(
            "request",
            lambda r: request_log.append((time.monotonic(), r.method, r.url)),
        )
        page.on(
            "requestfailed",
            lambda r: failed_reqs.append(
                (f"{r.method} {r.url}", r.failure or "<no reason>")
            ),
        )

        def goto(url: str, label: str) -> None:
            t0 = time.monotonic()
            print(f"\n=== {label}: GET {url} ===")
            page.goto(url, wait_until="domcontentloaded", timeout=10_000)
            # Wait briefly for the async init() to fire its fetches.
            try:
                page.wait_for_function(
                    "document.querySelectorAll('#set-nav a').length > 0",
                    timeout=4_000,
                )
                ok = True
            except Exception as e:
                ok = False
                print(f"  set-nav never populated: {e}")
            tiles = page.locator(".tile").count()
            nav_links = page.locator("#set-nav a").count()
            status = page.locator("#status").text_content() or ""
            print(f"  set-nav links: {nav_links}, tiles: {tiles}, status='{status.strip()}', dom-ready: {ok}")
            print(f"  elapsed: {(time.monotonic() - t0):.2f}s")

        DWELL_MS = 3500

        def report_state(label: str) -> None:
            nav = page.locator("#set-nav a").count()
            tiles = page.locator(".tile").count()
            sse_count = page.evaluate(
                "performance.getEntriesByType('resource')"
                ".filter(e => e.name.includes('/api/inference/live')).length"
            )
            body_class = page.locator("body").get_attribute("class") or ""
            print(
                f"  {label}  nav={nav} tiles={tiles} sse_on_page={sse_count}"
                f" body=[{body_class}]"
            )

        # Cycle 0: load set1 fresh (labels off), confirm clean baseline.
        goto(f"{BASE}/sets/set1", "cycle 0: set1 (labels off)")
        page.wait_for_timeout(DWELL_MS)
        report_state("after dwell")

        # Toggle labels ON. Now navigate around and see if buttons / video
        # state survives. We reset localStorage explicitly to be sure the
        # NEW behavior — every fresh page resets to off — is in effect.
        page.click("#labels-toggle")
        print("\n=== clicked Show labels ===")
        page.wait_for_timeout(800)
        report_state("post-click")

        for i in range(8):
            target = "set2" if i % 2 == 0 else "set1"
            goto(f"{BASE}/sets/{target}", f"cycle {i+1}: {target}")
            page.wait_for_timeout(DWELL_MS)
            report_state("after dwell")
            if page.locator("#set-nav a").count() < 2:
                print(f"  >>> DEGRADED at cycle {i+1} <<<")
                print("  set-nav HTML:", page.locator("#set-nav").inner_html())
                break

        # Final state on the third page.
        print("\n=== final state on 3rd page ===")
        print("  set-nav HTML:", page.locator("#set-nav").inner_html()[:200])
        print("  grid tiles:", page.locator(".tile").count())
        print("  body class:", page.locator("body").get_attribute("class"))

        # Snapshot any in-flight requests by examining the resource
        # timing entries — anything without a responseEnd is still
        # pending.
        pending_at_end = page.evaluate(
            """
            performance.getEntriesByType('resource')
                .filter(e => e.responseEnd === 0)
                .map(e => `${e.initiatorType} ${e.name}`)
            """
        )

        browser.close()

    print("\n=== diagnostics ===")
    print(f"console errors/warnings: {len(console_errs)}")
    for e in console_errs[:20]:
        print(f"  {e}")
    print(f"page errors: {len(page_errs)}")
    for e in page_errs[:10]:
        print(f"  {e}")
    print(f"failed requests: {len(failed_reqs)}")
    for url, why in failed_reqs[:20]:
        print(f"  {url}  ({why})")
    print(f"pending at end (no responseEnd): {len(pending_at_end)}")
    for x in pending_at_end[:20]:
        print(f"  {x}")

    # Summary of request volume by URL family.
    families = Counter()
    for _, _, url in request_log:
        path = url.replace(BASE, "")
        if path.startswith("/api/inference/live"):
            families["sse:/api/inference/live"] += 1
        elif path.startswith("/api/events"):
            families["api:/api/events*"] += 1
        elif path.startswith("/api/"):
            families[f"api:{path.split('?', 1)[0]}"] += 1
        elif path.startswith("/hls/"):
            families["hls:/hls/*"] += 1
        elif path.startswith("/static/"):
            families[f"static:{path.split('?', 1)[0]}"] += 1
        else:
            families[path.split("?", 1)[0]] += 1
    print("\n=== request volume ===")
    for k, n in families.most_common():
        print(f"  {n:>3}  {k}")

    return 0 if not failed_reqs and not page_errs else 1


if __name__ == "__main__":
    sys.exit(main())
