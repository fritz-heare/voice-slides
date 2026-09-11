#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["playwright"]
# ///
"""Offline check of the two views: presenter, popout, and the sync between them.

Nothing is mocked at the renderer — this drives the real pages in a real browser
through the same seam the server uses, the WebSocket. The socket is stubbed in
the page before its module script runs, so no STT, no inference, and no token:
fixture frames go in, DOM comes out.

Every request the browser makes is counted, and any request that leaves this
origin fails the run. That is the check that matters for the popout, whose whole
claim is that it presents with the network unplugged.

    uv run test_views.py            # add --shots to write docs/img/v3-*.png
"""
from __future__ import annotations

import asyncio
import functools
import http.server
import json
import pathlib
import socketserver
import sys
import threading

HERE = pathlib.Path(__file__).parent
STATIC = HERE / "static"
PORT = 8972
ORIGIN = f"http://127.0.0.1:{PORT}"
SHOTS = HERE / "docs" / "img"

# One deck exercising the whole v3 vocabulary: a diagram, a chart, a meme that
# is switched off, a two-column layout, and a presenter note.
DECK = """---
theme: default
title: Phrase Breaks
layout: cover
---

# Phrase Breaks

Why the deck moves before you stop talking

---
layout: section
---

# How a pass gets scheduled

---

# The pipeline

```mermaid
graph LR
  MIC[microphone] --> STT[streaming STT]
  STT --> BUF[pending buffer]
  BUF -->|phrase break| FMT[formatting pass]
  FMT --> DECK[deck]
  DECK --> POP[popout]
```

<!--
The buffer is the only stateful bit. Say so out loud.
-->

---

# Where the wait goes

<Chart type="bar" title="Pause before a pass" unit="ms"
       :data="[['full stop', 700], ['comma', 1200], ['dangling word', 1800]]" />

---
layout: statement
---

# Four seconds, worst case
"""

CONFIG = {
    "type": "config", "deck_path": "/tmp/slides.md", "model": "claude-haiku-4-5",
    "modality": "dictate", "theme": "default",
    "features": ["charts", "memes", "diagrams", "images", "themes",
                 "navigation", "incremental"],
    "phrase_pause_s": 0.7, "continuous_max_s": 4.0,
    "modalities": [
        {"id": "dictate", "label": "Close dictation", "hint": "near-verbatim", "tools": []},
        {"id": "highlights", "label": "Highlights + sources", "hint": "condensed",
         "tools": ["fetch_url"]},
        {"id": "spicy", "label": "Spicy", "hint": "opinions", "tools": []},
    ],
    "themes": [
        {"id": "default", "label": "Default", "blurb": "indigo on near-white"},
        {"id": "parchment", "label": "Parchment", "blurb": "warm archival paper"},
        {"id": "slate", "label": "Slate", "blurb": "cool grey-blue"},
        {"id": "verdigris", "label": "Verdigris", "blurb": "aged copper green"},
        {"id": "brass", "label": "Brass", "blurb": "warm amber and gold"},
        {"id": "noir", "label": "Noir", "blurb": "monochrome"},
        {"id": "solar", "label": "Solar", "blurb": "orange and cyan"},
    ],
    "feature_list": [
        {"id": "charts", "label": "Charts", "hint": "bar and line"},
        {"id": "memes", "label": "Memes", "hint": "memegen"},
        {"id": "diagrams", "label": "Diagrams", "hint": "mermaid fences"},
        {"id": "images", "label": "Images", "hint": "remote images"},
        {"id": "themes", "label": "Themes", "hint": "agent may repaint"},
        {"id": "navigation", "label": "Agent navigation", "hint": "agent may move the slide"},
        {"id": "incremental", "label": "Live updates", "hint": "reformat at phrase breaks"},
    ],
}

# The stub. `new WebSocket(...)` never opens anything; the page's own onopen /
# onmessage handlers are driven from the test instead.
STUB = """
window.__sent = [];
window.__ws = null;
class FakeSocket {
  constructor(url) {
    this.url = url; this.readyState = 0; this.binaryType = 'blob';
    window.__ws = this;
    setTimeout(() => { this.readyState = 1; this.onopen && this.onopen({}); }, 0);
  }
  send(d) { if (typeof d === 'string') window.__sent.push(JSON.parse(d)); }
  close() { this.readyState = 3; }
}
window.WebSocket = FakeSocket;
window.__recv = m => window.__ws.onmessage({ data: JSON.stringify(m) });
// Nothing here should ever ask for the microphone; if it does, fail loudly
// rather than hang on a permission prompt.
navigator.mediaDevices = { getUserMedia: () => Promise.reject(new Error('no mic in the harness')) };
"""

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" + (f"\n       {detail}" if not ok and detail else ""))


def serve() -> socketserver.TCPServer:
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(STATIC))
    socketserver.TCPServer.allow_reuse_address = True
    httpd = socketserver.TCPServer(("127.0.0.1", PORT), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


async def main(shots: bool = False) -> int:
    from playwright.async_api import async_playwright

    httpd = serve()
    external: list[str] = []
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            ctx = await browser.new_context(viewport={"width": 1480, "height": 900})
            await ctx.add_init_script(STUB)

            # Every request is either same-origin or a finding. Aborting rather
            # than allowing means a page that depends on the network fails here
            # instead of passing on a machine that happens to have it.
            async def gate(route):
                url = route.request.url
                if url.startswith(ORIGIN) or url.startswith("data:") or url.startswith("blob:"):
                    await route.continue_()
                else:
                    external.append(url)
                    await route.abort()
            await ctx.route("**/*", gate)

            page = await ctx.new_page()
            page.on("pageerror", lambda e: FAIL.append(f"pageerror: {e}"))
            page.on("console", lambda m: FAIL.append(f"console.{m.type}: {m.text}")
                    if m.type == "error" else None)
            await page.goto(f"{ORIGIN}/index.html")
            await page.wait_for_function("window.__ws && window.__ws.readyState === 1")

            # -- the presenter view ------------------------------------------
            hello = await page.evaluate("window.__sent")
            check("the page opens with a hello", any(m["type"] == "hello" for m in hello),
                  str(hello))

            await page.evaluate("m => window.__recv(m)", CONFIG)
            await page.wait_for_function("document.querySelectorAll('#palettes label').length === 7")
            check("the theme picker is built from the config frame", True)
            feats = await page.eval_on_selector_all("[data-feat]", "els => els.length")
            check("a toggle per feature", feats == 7, f"got {feats}")

            for utter, settled in [("This talk is about phrase breaks.", True),
                                   ("The idea is that the deck should move while you are", False),
                                   ("still talking, so you can see it land.", True)]:
                await page.evaluate("m => window.__recv(m)", {"type": "final", "text": utter})
                await page.evaluate("m => window.__recv(m)", {
                    "type": "agent", "role": "dictation", "text": utter, "settled": settled})
            await page.evaluate("m => window.__recv(m)", {
                "type": "status", "text": "formatting…", "busy": True, "queued": 34,
                "tools": 1, "settled": False})
            said = await page.eval_on_selector_all("#transcript p", "e => e.length")
            check("the full captured transcript accumulates", said == 3, f"got {said}")
            turns = await page.eval_on_selector_all("#agent .turn", "e => e.length")
            check("the agent transcript accumulates", turns == 3, f"got {turns}")
            open_turns = await page.eval_on_selector_all("#agent .turn.open", "e => e.length")
            check("a mid-phrase pass is marked open in the agent pane",
                  open_turns == 1, f"got {open_turns}")
            work = await page.text_content("#status")
            check("in-flight background work is on the status line",
                  "formatting" in work and "34w queued" in work and "1 lookup" in work, work)

            await page.evaluate("m => window.__recv(m)", {
                "type": "deck", "markdown": DECK, "pass_ms": 1840, "settled": True})
            await page.wait_for_selector("#deck .slide")
            n = await page.eval_on_selector_all("#deck .slide", "e => e.length")
            check("every slide previews", n == 5, f"got {n}")

            # -- mermaid, from the vendored bundle on this origin -------------
            await page.wait_for_selector("#deck .vs-mermaid.ready svg", timeout=45000)
            check("the mermaid fence rendered as a diagram", True)
            left = await page.eval_on_selector_all("#deck .vs-mermaid.ready .src",
                                                   "e => e.length")
            check("the fence source gives way to the diagram", left == 0, f"got {left}")
            bars = await page.eval_on_selector_all("#deck .vs-chart .bar", "e => e.length")
            check("the chart drew a bar per pair", bars == 3, f"got {bars}")

            # -- the popout ---------------------------------------------------
            async with ctx.expect_page() as popped:
                await page.click("#popout")
            stage = await popped.value
            stage.on("pageerror", lambda e: FAIL.append(f"popout pageerror: {e}"))
            await stage.wait_for_selector(".stage-wrap .slide .content h1")
            await stage.wait_for_function("document.querySelectorAll('#slide .num').length === 1")
            pos = await stage.text_content("#slide .num")
            check("the popout opens on the slide the presenter is showing",
                  pos.strip() == "5 / 5", pos)
            one = await stage.eval_on_selector_all(".stage-wrap .slide", "e => e.length")
            check("the popout draws exactly one slide", one == 1, f"got {one}")
            chrome = await stage.eval_on_selector_all(
                "header, #transcript, #agent, .badge", "e => e.length")
            check("the popout carries no presenter chrome", chrome == 0, f"got {chrome}")

            # -- the agent's navigation directive, honoured in both windows ---
            await page.evaluate("m => window.__recv(m)", {"type": "nav", "action": "goto",
                                                          "index": 4})
            await stage.wait_for_function(
                "document.querySelector('#slide .num').textContent.trim() === '4 / 5'")
            check("an @vs goto moves the presenter and the popout together", True)
            active = await page.eval_on_selector_all(
                "#deck .slide", "els => els.findIndex(e => e.classList.contains('active'))")
            check("the presenter highlight follows the directive", active == 3, f"got {active}")

            # The projector end can drive too, and the presenter follows.
            await stage.keyboard.press("ArrowLeft")
            await page.wait_for_function(
                "document.getElementById('pos').textContent.trim() === '3 / 5'")
            check("navigating from the popout moves the presenter", True)

            # -- a spoken theme change ---------------------------------------
            await page.evaluate("m => window.__recv(m)", {"type": "theme", "theme": "parchment",
                                                          "source": "agent"})
            await stage.wait_for_function(
                "document.documentElement.getAttribute('data-palette') === 'parchment'")
            check("a spoken theme change reaches both windows", True)
            accent = await stage.evaluate(
                "getComputedStyle(document.documentElement).getPropertyValue('--accent').trim()")
            check("the palette actually changed the accent", accent == "#7b2d26", accent)

            # -- a feature switched off stops rendering AND stops being offered
            await page.evaluate("m => window.__recv(m)", {
                "type": "features",
                "features": [f for f in CONFIG["features"] if f != "diagrams"]})
            await page.wait_for_function(
                "document.querySelectorAll('#deck .vs-mermaid').length === 0")
            check("switching diagrams off falls back to the fence text", True)

            if shots:
                SHOTS.mkdir(parents=True, exist_ok=True)
                await page.evaluate("m => window.__recv(m)", {
                    "type": "features", "features": CONFIG["features"]})
                await page.evaluate("m => window.__recv(m)", {"type": "theme",
                                                              "theme": "default"})
                await page.wait_for_selector("#deck .vs-mermaid.ready svg")
                # Park both windows on the diagram, which is the slide worth
                # photographing: it is the one the renderer could not draw before.
                await page.evaluate("m => window.__recv(m)", {"type": "nav",
                                                              "action": "goto", "index": 3})
                await stage.wait_for_selector("#slide .vs-mermaid.ready svg")
                await asyncio.sleep(0.6)
                await page.screenshot(path=str(SHOTS / "v3-presenter.png"))
                await stage.screenshot(path=str(SHOTS / "v3-popout.png"))
                await page.evaluate("m => window.__recv(m)", {"type": "theme", "theme": "noir"})
                await page.evaluate("document.documentElement.setAttribute('data-theme','dark')")
                await page.click("#featbtn")
                await asyncio.sleep(0.3)
                await page.screenshot(path=str(SHOTS / "v3-features.png"))
                await page.click("#themebtn")
                await asyncio.sleep(0.3)
                await page.screenshot(path=str(SHOTS / "v3-themes.png"))
                print(f"[shots] wrote {SHOTS}/v3-*.png")

            await browser.close()
    finally:
        httpd.shutdown()

    check("nothing left this origin", not external, f"{len(external)}: {external[:4]}")
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        for f in FAIL:
            print("  - " + f)
    print(f"external requests: {len(external)}   (inference calls: 0 by construction — "
          "the socket is stubbed and the only origin reachable is the static server)")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(shots="--shots" in sys.argv)))
