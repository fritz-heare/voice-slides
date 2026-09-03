#!/usr/bin/env python3
"""End-to-end check against the real services.

Starts app.py, fetches the page over its own HTTP fallback, then plays the role
of the browser on /ws: synthesizes speech with heare-speech-services' TTS, sends
it up as 16 kHz PCM16 frames, ends the turn, and waits for the deck.

Nothing is mocked. Requires the tailnet (pook) and the local credential store.

    python3 test_e2e.py
"""
from __future__ import annotations

import array
import asyncio
import audioop
import io
import json
import os
import subprocess
import sys
import time
import urllib.request
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("E2E_PORT", "8791"))
SPEECH = "http://pook.tail5ae4b.ts.net"
SPEECH_HOST = "heare-speech-services"

SCRIPT = [
    "This talk is called streaming transcription latency.",
    "New slide. The problem is that Whisper is a bidirectional encoder, "
    "so you cannot decode incrementally.",
]


def tts(text: str) -> bytes:
    """16 kHz mono PCM16 of `text`, via the same service the app transcribes with."""
    req = urllib.request.Request(
        f"{SPEECH}/tts/synthesize", data=text.encode(),
        headers={"Host": SPEECH_HOST, "content-type": "text/plain"})
    with urllib.request.urlopen(req, timeout=120) as r:
        wav = r.read()
    with wave.open(io.BytesIO(wav)) as w:
        pcm = w.readframes(w.getnframes())
        rate, width, ch = w.getframerate(), w.getsampwidth(), w.getnchannels()
    out, _ = audioop.ratecv(pcm, width, ch, rate, 16000, None)
    return out


async def main() -> int:
    import websockets

    env = {**os.environ, "PORT": str(PORT), "BIND": "127.0.0.1",
           "DECK_PATH": "/tmp/voice-slides-e2e/slides.md"}
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"):
        env.pop(var, None)
    proc = subprocess.Popen([sys.executable, os.path.join(HERE, "app.py")], env=env)
    try:
        # -- the page itself, over websockets' HTTP fallback -----------------
        page = None
        for _ in range(40):
            try:
                page = urllib.request.urlopen(f"http://127.0.0.1:{PORT}/", timeout=2).read()
                break
            except Exception:
                await asyncio.sleep(0.25)
        assert page and b"voice-slides" in page, "page did not serve"
        print(f"[http] GET / -> {len(page)} bytes, contains the app shell")

        code = urllib.request.urlopen(
            urllib.request.Request(f"http://127.0.0.1:{PORT}/nope.txt"), timeout=5
        ) if False else None
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/../app.py", timeout=5)
            print("[http] TRAVERSAL NOT BLOCKED")
            return 1
        except urllib.error.HTTPError as e:
            print(f"[http] GET /../app.py -> {e.code} (traversal blocked)")

        # -- the browser's half of /ws ---------------------------------------
        deck = None
        finals: list[str] = []
        got_deck = asyncio.Event()
        all_final = asyncio.Event()
        async with websockets.connect(f"ws://127.0.0.1:{PORT}/ws", max_size=None) as ws:
            async def pump():
                nonlocal deck
                async for raw in ws:
                    m = json.loads(raw)
                    if m["type"] == "partial" and m.get("text"):
                        print(f"  [partial] {m['text'][:70]}")
                    elif m["type"] == "final":
                        finals.append(m["text"])
                        print(f"  [FINAL]   {m['text']}")
                        if len(finals) == len(SCRIPT):
                            all_final.set()
                    elif m["type"] == "deck":
                        deck = m["markdown"]
                        print(f"  [deck]    {m['pass_ms']}ms, {len(deck)} bytes")
                        got_deck.set()
                    elif m["type"] == "error":
                        print(f"  [error]   {m['message']}")
                        got_deck.set()

            task = asyncio.create_task(pump())
            for line in SCRIPT:
                pcm = tts(line)
                print(f"[ws] sending {len(pcm)/32000:.1f}s: {line[:50]}…")
                for i in range(0, len(pcm), 3200):     # 100 ms frames
                    await ws.send(pcm[i:i + 3200])
                    await asyncio.sleep(0.02)
                await ws.send(json.dumps({"type": "end"}))
                await asyncio.sleep(0.5)
            # A pass may already have fired on the debounce mid-script. The one
            # that matters is the pass that runs after the LAST final, so wait
            # for every final to land before arming the wait.
            await asyncio.wait_for(all_final.wait(), timeout=120)
            got_deck.clear()
            await ws.send(json.dumps({"type": "flush"}))
            await asyncio.wait_for(got_deck.wait(), timeout=180)
            task.cancel()

        assert len(finals) == len(SCRIPT), f"expected {len(SCRIPT)} finals, got {len(finals)}"

        if not deck:
            print("FAIL: no deck came back")
            return 1
        print("\n--- deck ---")
        print(deck)
        assert deck.startswith("---"), "deck has no headmatter"
        assert deck.count("\n---") >= 2, "deck has no second slide"
        assert "encoder" in deck.lower(), "second utterance never reached the deck"
        on_disk = open("/tmp/voice-slides-e2e/slides.md").read()
        assert on_disk.strip() == deck.strip(), "DECK_PATH does not match what was sent"
        print("--- DECK_PATH matches the deck sent to the browser ---")
        return 0
    finally:
        proc.terminate()
        proc.wait(timeout=10)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
