# /// script
# requires-python = ">=3.11"
# dependencies = ["websockets>=14"]
# ///
"""voice-slides: dictate a Slidev deck, an agent formats it while you talk.

One process, one page, one WebSocket. The socket carries two kinds of frame in
one direction and five in the other:

  browser -> app   binary PCM16 mono @16 kHz frames, plus JSON control frames
                   ({"type":"end"} end-of-utterance, "flush", "reset", "deck",
                   "modality").
  app -> browser   {"type":"partial"} live caption text, {"type":"final"} the
                   settled utterance, {"type":"deck"} the Slidev markdown,
                   {"type":"status"}, {"type":"tool"} a fetch in flight,
                   {"type":"config"}, {"type":"error"}.

The app is a relay with one piece of judgement in it. Audio goes straight up to
heare-speech-services' /stt/stream and the provisional partials come straight
back down, so the caption tracks the voice at ~400 ms. Finals accumulate into a
pending buffer; when dictation pauses (or the buffer grows past a word count),
one Haiku call folds the pending text into the running deck and the new deck
markdown goes down the same socket.

The deck is Slidev markdown, and every pass also writes it to DECK_PATH. That
file is the interface to Slidev proper: `npx slidev <DECK_PATH>` renders the
same deck with hot reload, without this process depending on Node. The Vue
components the deck may reference are mirrored to DECK_PATH's `components/`
alongside it, which is where Slidev auto-imports them from.

Three modalities decide how much licence the formatting pass has: `dictate`
stays near-verbatim, `highlights` condenses and may fetch supporting data with
a tool, `spicy` invents the backdrop. A modality is a prompt plus a tool set;
that is the whole mechanism.

There is no HTTP API beyond the page itself. Export is a Blob download in the
browser, so the deck never needs a round trip to leave.

Env:
  BIND PORT DECK_PATH
  SPEECH_URL SPEECH_HOST          heare-speech-services location (Host-routed)
  ANTHROPIC_BASE_URL              inference endpoint; defaults to the ant-proxy
  ANTHROPIC_AUTH_TOKEN            bearer for that endpoint
  MODEL                           Anthropic model id
  CLEANUP_DEBOUNCE_S CLEANUP_MAX_WORDS
  DEFAULT_MODALITY                dictate | highlights | spicy
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import logging
import mimetypes
import os
import pathlib
import re
import shutil
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlsplit

import websockets
from websockets.asyncio.server import ServerConnection, serve
from websockets.datastructures import Headers
from websockets.http11 import Response

log = logging.getLogger("voice-slides")

BIND = os.environ.get("BIND", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8097"))

# Canonical access to heare-speech-services is Host-header routing on port 80.
# piku reassigns the app's real port on every restart, so an IP:port here is a
# reference that expires; the vhost name does not.
SPEECH_URL = os.environ.get("SPEECH_URL", "http://pook.tail5ae4b.ts.net").rstrip("/")
SPEECH_HOST = os.environ.get("SPEECH_HOST", "heare-speech-services")


# ant-proxy holds the credential, refreshes it, and stamps the Authorization
# header on the way upstream. This process never sees a token.
ANTHROPIC_BASE_URL = os.environ.get("ANTHROPIC_BASE_URL", "http://localhost:8787").rstrip("/")
ANTHROPIC_URL = f"{ANTHROPIC_BASE_URL}/v1/messages"
ANTHROPIC_AUTH_TOKEN = os.environ.get("ANTHROPIC_AUTH_TOKEN", "proxied")
MODEL = os.environ.get("MODEL", "claude-haiku-4-5-20251001")

# Silence after a final before the deck is rewritten. Long enough that a speaker
# drawing breath mid-thought does not trigger a pass, short enough that the
# preview lands while the thought is still in the room.
CLEANUP_DEBOUNCE_S = float(os.environ.get("CLEANUP_DEBOUNCE_S", "2.5"))
# ...and a ceiling, so someone who never pauses still sees the deck move.
CLEANUP_MAX_WORDS = int(os.environ.get("CLEANUP_MAX_WORDS", "110"))

HERE = pathlib.Path(__file__).parent
STATIC = HERE / "static"
CHEATSHEET = HERE / "slidev-cheatsheet.md"
# Vue single-file components the deck may reference. Slidev auto-imports from a
# `components/` directory beside the deck file, so these are copied there.
COMPONENTS_SRC = HERE / "slidev" / "components"

# Ceilings on the tool loop. Three rounds is enough for fetch, read, fetch
# again; past that the pass is taking longer than the speaker will wait.
MAX_TOOL_ROUNDS = 3
FETCH_TIMEOUT_S = 15
FETCH_MAX_BYTES = 200_000
FETCH_MAX_CHARS = 6_000

# Where the running deck is mirrored. Slidev's dev server watches its input
# file and hot-reloads, so pointing `npx slidev` at this path gives the real
# renderer alongside the built-in preview — without this process owning a Node
# toolchain. Nothing here reads the file back; it is an output.
DECK_PATH = pathlib.Path(
    os.environ.get("DECK_PATH", os.path.expanduser("~/.local/share/voice-slides/slides.md"))
)

# The job, minus the licence. Everything here holds in every modality; what
# differs between them is how much of the speaker's phrasing survives, and
# whether the pass may go and get something.
CONTRACT = """\
You maintain a Slidev deck that a person is dictating out loud, one utterance \
at a time. You receive the deck as it currently stands and the raw \
speech-to-text of what they just said. You return the whole deck, updated.

Output contract:
- Output ONLY the deck markdown. No preamble, no wrapping code fence, no \
commentary. The output is written to slides.md and rendered as-is.
- The output must be valid Slidev markdown per the reference above.

Deck conventions:
- The file opens with headmatter. Keep it minimal: `theme: default`, a `title:` \
matching the cover heading, and `transition: slide-left`.
- The first slide is the cover (`layout: cover` in the headmatter): an `# H1` \
and at most one subtitle line.
- Content slides are a `# H1` heading plus up to six `-` bullets. Reach for \
`layout: section` when the speaker announces a new section, `layout: two-cols` \
when they contrast two things, `layout: statement` or `layout: fact` when they \
land a single point or number. Do not decorate every slide with a layout.
- Use a fenced code block when the speaker dictates code or config.
- `<Chart>` and `<Meme>` are available (see the component reference above). \
At most one component on a slide, and only when the content asks for it.
- Put an aside the speaker clearly meant as an aside into a presenter note \
(a trailing HTML comment), not onto the slide.

Spoken structure commands are instructions, not content: "new slide", \
"next slide", "title this X", "make that a bullet", "scratch that", \
"go back and fix that", "put that in two columns", "add a note". Obey them and \
never transcribe them onto a slide. The transcript is speech-to-text, so those \
commands arrive damaged: "new side", "new slight", "next side" at the start of \
an utterance mean "new slide". Read the command through the mis-transcription.

Fix obvious transcription damage to technical vocabulary when context makes the \
intent unambiguous. Preserve earlier slides unless the new speech revises them; \
append by default. An empty deck plus the first utterance means: write the \
headmatter and the cover slide.
"""

DICTATE = """\
MODALITY: close dictation. You are a typist with good judgement, not an editor.

- Keep the speaker's words. What lands on the slide is their sentences, their \
terminology, their order.
- Strip only what nobody meant to say: filler ("um", "you know", "so \
basically", "right"), false starts, and self-corrections.
- Do not summarise, compress, re-title, or improve. If they made the point in a \
long sentence, the bullet is a long sentence.
- Do not invent facts, numbers, names, claims, or supporting material. Nothing \
reaches a slide that was not said.
"""

HIGHLIGHTS = """\
MODALITY: highlights and supporting material. You are an editor building the \
deck the talk deserves.

- Condense. A minute of speech is three or four terse bullets, not a \
transcript. Keep the speaker's terminology; drop their sentence structure.
- Title each slide for what it is about, not with the speaker's first words.
- When a point rests on something checkable — a version number, a date, a \
published figure, a definition — call `fetch_url` and put the supporting number \
or quote on the slide. Prefer sources you are confident exist: \
`https://en.wikipedia.org/api/rest_v1/page/summary/<Title>` for definitions and \
background, official documentation and status pages otherwise.
- At most two fetches per pass. If a fetch fails or returns nothing useful, \
carry on without it — never stall the deck on a fetch.
- Reach for a `<Chart>` when the speaker gives you three or more numbers that \
compare.
- Everything on a slide is either something the speaker said or something you \
fetched this pass. Never a number you remembered.
"""

SPICY = """\
MODALITY: spicy. You are the backdrop of a conference talk given by somebody \
funnier than they realise.

- The speaker's substance stays true: their facts, numbers, names, and claims \
go on the slide unchanged. The garnish is yours.
- Slide titles can be jokes. Bullets can be sassy. A well-placed `<Meme>` beats \
a paragraph, and `layout: statement` beats a bullet list when the line lands.
- Punch at the subject matter, at the industry, and at the speaker's own \
premise — never at a named person.
- One joke per slide. A deck where everything is a joke is a deck where nothing \
is.
- Invent the framing, never the evidence. If the speaker did not say a number, \
it is not on the slide.
"""


@dataclass(frozen=True)
class Modality:
    """A modality is a prompt plus a tool set. There is no other mechanism."""

    id: str
    label: str
    hint: str
    instructions: str
    tools: tuple[str, ...] = ()


MODALITIES: dict[str, Modality] = {
    m.id: m for m in (
        Modality("dictate", "Close dictation",
                 "Near-verbatim. Your words, tidied.", DICTATE),
        Modality("highlights", "Highlights + sources",
                 "Condensed to key points, with data fetched to back them up.",
                 HIGHLIGHTS, tools=("fetch_url",)),
        Modality("spicy", "Spicy",
                 "Your facts, a backdrop with opinions.", SPICY),
    )
}

DEFAULT_MODALITY = os.environ.get("DEFAULT_MODALITY", "dictate")
if DEFAULT_MODALITY not in MODALITIES:
    DEFAULT_MODALITY = "dictate"


def build_system_prompt(modality: str = DEFAULT_MODALITY) -> str:
    """Slidev reference first, then the job, then the licence.

    The reference is a repo file rather than a string literal so it can be
    corrected against sli.dev without touching code, and so the prompt the model
    actually saw is reviewable as a document.
    """
    reference = CHEATSHEET.read_text(encoding="utf-8")
    mode = MODALITIES.get(modality) or MODALITIES[DEFAULT_MODALITY]
    return (
        "You are the formatting pass of a voice-dictated slide composer. "
        "You write Slidev markdown. Reference:\n\n"
        f"<slidev-reference>\n{reference}\n</slidev-reference>\n\n"
        f"{CONTRACT}\n"
        f"{mode.instructions}"
    )


# --------------------------------------------------------------------------
# heare-speech-services
# --------------------------------------------------------------------------

def _stt_stream_target() -> tuple[str, str, int]:
    """WebSocket URI plus the real TCP target for /stt/stream.

    The vhost goes in the URI so the library derives one correct Host header;
    passing it via additional_headers instead sends a duplicate and nginx
    answers 400. host=/port= then aim the connection at the machine.
    """
    parts = urlsplit(SPEECH_URL)
    scheme = "wss" if parts.scheme == "https" else "ws"
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return f"{scheme}://{SPEECH_HOST}/stt/stream", parts.hostname or "", port


class SttStream:
    """The upstream /stt/stream socket, held open across utterances.

    /stt/stream is multi-turn: audio frames accumulate, `{"type":"end"}` closes
    a turn with one authoritative final, and the next frame starts the next
    turn on the same connection. Reconnecting per utterance would pay the
    handshake on every pause.
    """

    def __init__(self, on_partial, on_final):
        self._ws = None
        self._reader: asyncio.Task | None = None
        self._on_partial = on_partial
        self._on_final = on_final

    async def connect(self) -> None:
        uri, host, port = _stt_stream_target()
        self._ws = await websockets.connect(
            uri, host=host, port=port, open_timeout=20, max_size=None
        )
        # No {"type":"config"} frame: /stt/stream accepts a vocabulary bias, and
        # biasing toward deck vocabulary ("slide", "layout", "bullet") measurably
        # made this worse. On "New slide. The problem is that Whisper is a
        # bidirectional encoder." the service default returns that sentence with
        # casing and punctuation intact; a deck-vocabulary bias returns
        # "new side the problem is that whisper is a bidirectional encoder" — it
        # does not recover "slide" AND it costs the sentence boundaries the
        # formatting pass reads structure from.
        self._reader = asyncio.create_task(self._read())

    async def _read(self) -> None:
        try:
            async for raw in self._ws:
                msg = json.loads(raw)
                kind = msg.get("type")
                if kind == "partial":
                    await self._on_partial(msg.get("text") or "")
                elif kind == "final":
                    await self._on_final((msg.get("text") or "").strip())
                elif msg.get("error"):
                    log.warning("stt error: %s", msg["error"])
        except websockets.ConnectionClosed:
            pass
        except Exception:
            log.exception("stt reader died")

    async def send_audio(self, frame: bytes) -> None:
        await self._ws.send(frame)

    async def end_turn(self) -> None:
        await self._ws.send(json.dumps({"type": "end"}))

    async def close(self) -> None:
        if self._reader:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader
        if self._ws:
            with contextlib.suppress(Exception):
                await self._ws.close()


# --------------------------------------------------------------------------
# tools — what the highlights modality is allowed to go and get
# --------------------------------------------------------------------------

FETCH_URL_TOOL = {
    "name": "fetch_url",
    "description": (
        "Fetch a public web page or JSON API and return it as plain text. Use it to "
        "check something the speaker's point rests on — a version number, a date, a "
        "published figure, a definition — before it goes on a slide. Returns at most "
        "a few thousand characters of the document."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Absolute http(s) URL."},
            # Not decoration: the speaker sees this while the fetch is in flight,
            # and a bare URL in a loading chip reads as noise.
            "why": {
                "type": "string",
                "description": "Five words on what you are checking. Shown to the "
                               "speaker while the fetch runs.",
            },
        },
        "required": ["url", "why"],
    },
}

TOOL_SPECS: dict[str, dict[str, Any]] = {"fetch_url": FETCH_URL_TOOL}

_TAGS = re.compile(r"(?is)<(script|style)\b.*?</\1>|<[^>]+>")


def _assert_public(url: str) -> None:
    """Reject anything that is not a public http(s) endpoint.

    The URL is model output derived from a live microphone, and this process
    sits on a tailnet beside an inference proxy that does not authenticate its
    callers. The RESOLVED address is what gets checked, not the hostname, so a
    name that answers 127.0.0.1 is caught too.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise ValueError(f"scheme {parts.scheme!r} is not allowed")
    if not parts.hostname:
        raise ValueError("no host in url")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    for info in socket.getaddrinfo(parts.hostname, port, proto=socket.IPPROTO_TCP):
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global or ip.is_multicast:
            raise ValueError(f"{parts.hostname} resolves to non-public {ip}")


class _GuardedRedirect(urllib.request.HTTPRedirectHandler):
    """Re-check every hop. Without this the guard is one 302 away from useless."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _assert_public(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_GuardedRedirect)


def fetch_url(url: str, why: str = "") -> str:
    """Blocking GET of a public URL, flattened to text the model can read."""
    _assert_public(url)
    req = urllib.request.Request(url, headers={
        "user-agent": "voice-slides/1 (+https://github.com/fritz-heare/voice-slides)",
        "accept": "text/html, application/json;q=0.9, text/plain;q=0.8",
    })
    with _OPENER.open(req, timeout=FETCH_TIMEOUT_S) as resp:
        ctype = (resp.headers.get_content_type() or "").lower()
        charset = resp.headers.get_content_charset() or "utf-8"
        raw = resp.read(FETCH_MAX_BYTES)
    text = raw.decode(charset, "replace")
    if "json" not in ctype and "plain" not in ctype:
        text = _TAGS.sub(" ", text)
    text = re.sub(r"[ \t\u00a0]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text).strip()
    if len(text) > FETCH_MAX_CHARS:
        text = text[:FETCH_MAX_CHARS] + "\n…[truncated]"
    return text or "(empty response)"


TOOLS: dict[str, Callable[..., str]] = {"fetch_url": fetch_url}


def _run_tool(block: dict, on_tool) -> dict:
    """Execute one tool_use block and shape the tool_result that answers it.

    A tool that raises comes back as an is_error result rather than an
    exception: a fetch that 404s should cost the model one turn, not cost the
    speaker their deck update.
    """
    name = block.get("name") or ""
    args = block.get("input") or {}
    detail = str(args.get("why") or args.get("url") or name).strip()
    if on_tool:
        on_tool(name, detail, "running", "")
    try:
        if name not in TOOLS:
            raise ValueError(f"no such tool: {name}")
        content, is_error = TOOLS[name](**args), False
    except Exception as e:
        content, is_error = f"{type(e).__name__}: {e}", True
        log.warning("tool %s failed: %s", name, content)
    if on_tool:
        on_tool(name, detail, "error" if is_error else "done", content[:120] if is_error else "")
    return {
        "type": "tool_result",
        "tool_use_id": block.get("id"),
        "content": content,
        **({"is_error": True} if is_error else {}),
    }


# --------------------------------------------------------------------------
# the cleanup pass
# --------------------------------------------------------------------------

def _post(mode: Modality, messages: list[dict], with_tools: bool) -> dict:
    """One request to the messages API.

    ant-proxy drops the inbound Authorization header and substitutes its own,
    so the bearer sent here is a placeholder. It is real only when
    ANTHROPIC_BASE_URL points somewhere that authenticates its callers.
    """
    payload: dict[str, Any] = {
        "model": MODEL,
        "max_tokens": 4096,
        # Cached: the Slidev reference is ~2k tokens and identical on every pass
        # in a modality, and a pass fires every few sentences.
        "system": [{
            "type": "text",
            "text": build_system_prompt(mode.id),
            "cache_control": {"type": "ephemeral"},
        }],
        "messages": messages,
    }
    if with_tools and mode.tools:
        payload["tools"] = [TOOL_SPECS[t] for t in mode.tools if t in TOOL_SPECS]
    req = urllib.request.Request(ANTHROPIC_URL, data=json.dumps(payload).encode(), headers={
        "content-type": "application/json",
        "authorization": f"Bearer {ANTHROPIC_AUTH_TOKEN}",
        "anthropic-version": "2023-06-01",
    })
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read())


def _call_model(deck: str, dictation: str, modality: str = DEFAULT_MODALITY,
                on_tool=None) -> str:
    """Blocking Anthropic call. Runs in a thread; returns the new deck markdown.

    Modalities without tools settle in one request. A modality with tools may
    round-trip, so the loop runs the tools and hands the results back. The last
    round is always sent WITHOUT tools, which is what guarantees the pass ends
    in a deck rather than in another tool call the budget cannot pay for.
    """
    mode = MODALITIES.get(modality) or MODALITIES[DEFAULT_MODALITY]
    messages: list[dict] = [{"role": "user", "content": (
        f"CURRENT DECK:\n{deck or '(empty — no slides yet)'}\n\n"
        f"NEW DICTATION:\n{dictation}\n\n"
        "Return the full updated deck markdown."
    )}]
    out: dict = {}
    for round_ in range(MAX_TOOL_ROUNDS + 1):
        out = _post(mode, messages, with_tools=round_ < MAX_TOOL_ROUNDS)
        blocks = out.get("content") or []
        if out.get("stop_reason") != "tool_use":
            break
        uses = [b for b in blocks if b.get("type") == "tool_use"]
        if not uses:
            break
        messages.append({"role": "assistant", "content": blocks})
        messages.append({"role": "user", "content": [_run_tool(b, on_tool) for b in uses]})
    text = "".join(b.get("text", "") for b in (out.get("content") or [])
                   if b.get("type") == "text")
    return _strip_fence(text.strip())


def sync_components() -> None:
    """Mirror the Vue components into `components/` beside the deck.

    Slidev auto-imports components from that directory relative to the deck
    file, so a slide that says `<Chart/>` only renders if the .vue is sitting
    there. Copied rather than symlinked, so the deck directory stays something
    you can hand to somebody whole.
    """
    if not COMPONENTS_SRC.is_dir():
        return
    dst = DECK_PATH.parent / "components"
    dst.mkdir(parents=True, exist_ok=True)
    for src in sorted(COMPONENTS_SRC.glob("*.vue")):
        target = dst / src.name
        if not target.exists() or target.read_bytes() != src.read_bytes():
            shutil.copy2(src, target)


def write_deck(deck: str) -> None:
    """Mirror the deck to DECK_PATH for Slidev to pick up.

    Written via a temporary file and renamed, because Slidev's watcher fires on
    the write and a partially-flushed file parses as a broken deck.
    """
    DECK_PATH.parent.mkdir(parents=True, exist_ok=True)
    sync_components()
    tmp = DECK_PATH.with_suffix(".md.tmp")
    tmp.write_text(deck.rstrip() + "\n", encoding="utf-8")
    tmp.replace(DECK_PATH)


def _strip_fence(text: str) -> str:
    """Unwrap a fenced block the model wrapped the whole deck in.

    The system prompt forbids the fence; models emit one anyway often enough
    that the alternative is rendering ``` as a slide.
    """
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if len(lines) >= 2 and lines[-1].strip().startswith("```"):
        return "\n".join(lines[1:-1]).strip()
    return text


# --------------------------------------------------------------------------
# session
# --------------------------------------------------------------------------

class Session:
    """One browser tab: its STT socket, its transcript, its deck."""

    def __init__(self, ws: ServerConnection):
        self.ws = ws
        self.stt: SttStream | None = None
        self.transcript: list[str] = []
        self.pending: list[str] = []
        self.deck: str = ""
        self.modality: str = DEFAULT_MODALITY
        self._debounce: asyncio.Task | None = None
        self._pass: asyncio.Task | None = None
        self._again = False

    async def send(self, **msg: Any) -> None:
        with contextlib.suppress(websockets.ConnectionClosed):
            await self.ws.send(json.dumps(msg))

    # -- STT callbacks -----------------------------------------------------

    async def on_partial(self, text: str) -> None:
        await self.send(type="partial", text=text)

    async def on_final(self, text: str) -> None:
        if not text:
            await self.send(type="partial", text="")
            return
        self.transcript.append(text)
        self.pending.append(text)
        await self.send(type="final", text=text)
        words = sum(len(p.split()) for p in self.pending)
        await self._schedule(delay=0.0 if words >= CLEANUP_MAX_WORDS else CLEANUP_DEBOUNCE_S)

    # -- cleanup scheduling ------------------------------------------------

    async def _schedule(self, delay: float) -> None:
        if self._debounce:
            self._debounce.cancel()
        self._debounce = asyncio.create_task(self._after(delay))

    async def _after(self, delay: float) -> None:
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.sleep(delay)
            await self.run_pass()

    async def run_pass(self) -> None:
        """Fold `pending` into the deck. One pass at a time; a pass requested
        while another is in flight re-runs once the first lands, so speech that
        arrived mid-call is never dropped and never races the deck it edits."""
        if not self.pending:
            return
        if self._pass and not self._pass.done():
            self._again = True
            return
        self._pass = asyncio.create_task(self._do_pass())

    def _tool_reporter(self) -> Callable[..., None]:
        """Bridge the tool loop's progress back onto the socket.

        The loop runs in a worker thread, so each report is handed to the event
        loop rather than awaited. This is what puts a loading indicator in the
        preview while a fetch is in flight instead of a silent ten seconds.
        """
        loop = asyncio.get_running_loop()

        def report(name: str, detail: str, state: str, note: str = "") -> None:
            asyncio.run_coroutine_threadsafe(
                self.send(type="tool", name=name, detail=detail, state=state, note=note),
                loop)

        return report

    async def _do_pass(self) -> None:
        dictation = " ".join(self.pending)
        self.pending = []
        await self.send(type="status", text="formatting…")
        t0 = time.monotonic()
        try:
            deck = await asyncio.to_thread(
                _call_model, self.deck, dictation, self.modality, self._tool_reporter())
        except Exception as e:
            # Put the speech back at the front so the next pass still sees it,
            # in the order it was spoken.
            self.pending.insert(0, dictation)
            log.exception("cleanup pass failed")
            await self.send(type="error", message=f"cleanup pass failed: {type(e).__name__}")
            return
        finally:
            ms = int((time.monotonic() - t0) * 1000)
        if deck:
            self.deck = deck
            await asyncio.to_thread(write_deck, deck)
            await self.send(type="deck", markdown=deck, pass_ms=ms)
        await self.send(type="status", text="")
        if self._again:
            self._again = False
            await self.run_pass()

    async def close(self) -> None:
        for task in (self._debounce, self._pass):
            if task:
                task.cancel()
        if self.stt:
            await self.stt.close()


async def handler(ws: ServerConnection) -> None:
    session = Session(ws)
    try:
        session.stt = SttStream(session.on_partial, session.on_final)
        await session.stt.connect()
    except Exception as e:
        log.exception("cannot reach STT")
        await session.send(type="error", message=f"STT unreachable: {type(e).__name__}")
        return
    await session.send(type="status", text="ready")
    try:
        async for message in ws:
            if isinstance(message, bytes):
                await session.stt.send_audio(message)
                continue
            frame = json.loads(message)
            kind = frame.get("type")
            if kind == "end":
                await session.stt.end_turn()
            elif kind == "flush":
                await session.run_pass()
            elif kind == "deck":
                # The browser owns the deck text once the user edits it.
                session.deck = frame.get("markdown") or ""
            elif kind == "reset":
                session.transcript, session.pending, session.deck = [], [], ""
                await session.send(type="deck", markdown="", pass_ms=0)
            elif kind == "modality":
                wanted = frame.get("id")
                if wanted in MODALITIES:
                    session.modality = wanted
                    await session.send(type="status", text=MODALITIES[wanted].label.lower())
            elif kind == "hello":
                # The modality list ships from here so the picker and the prompts
                # cannot drift apart.
                await session.send(
                    type="config", deck_path=str(DECK_PATH), model=MODEL,
                    modality=session.modality,
                    modalities=[{"id": m.id, "label": m.label, "hint": m.hint,
                                 "tools": list(m.tools)} for m in MODALITIES.values()])
    except websockets.ConnectionClosed:
        pass
    finally:
        await session.close()


# --------------------------------------------------------------------------
# static serving — websockets' own HTTP fallback, so there is no second server
# --------------------------------------------------------------------------

def serve_static(connection: ServerConnection, request) -> Response | None:
    if request.path == "/ws":
        return None  # let the handshake proceed
    rel = "index.html" if request.path in ("/", "") else request.path.lstrip("/")
    target = (STATIC / rel).resolve()
    if not target.is_file() or STATIC.resolve() not in target.parents:
        return Response(404, "Not Found", Headers({"content-length": "0"}), b"")
    body = target.read_bytes()
    ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    return Response(200, "OK", Headers({
        "content-type": ctype,
        "content-length": str(len(body)),
        "cache-control": "no-store",
    }), body)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    sync_components()
    log.info("voice-slides on http://%s:%d  (stt=%s via %s, llm=%s, model=%s, deck=%s, "
             "modality=%s)",
             BIND, PORT, SPEECH_URL, SPEECH_HOST, ANTHROPIC_BASE_URL, MODEL, DECK_PATH,
             DEFAULT_MODALITY)
    async with serve(handler, BIND, PORT, process_request=serve_static, max_size=None):
        await asyncio.Future()


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
