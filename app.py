# /// script
# requires-python = ">=3.11"
# dependencies = ["websockets>=14"]
# ///
"""voice-slides: dictate a Slidev deck, an agent formats it while you talk.

One process, two pages, one WebSocket. The socket carries two kinds of frame in
one direction and eight in the other:

  browser -> app   binary PCM16 mono @16 kHz frames, plus JSON control frames
                   ({"type":"end"} end-of-utterance, "flush", "reset", "deck",
                   "modality", "theme", "feature").
  app -> browser   {"type":"partial"} live caption text, {"type":"final"} the
                   settled utterance, {"type":"deck"} the Slidev markdown,
                   {"type":"status"} with the in-flight background work,
                   {"type":"tool"} a fetch in flight, {"type":"agent"} one line
                   of the agent-side conversation, {"type":"nav"} a slide the
                   agent wants shown, {"type":"config"}, {"type":"error"}.

The app is a relay with one piece of judgement in it. Audio goes straight up to
heare-speech-services' /stt/stream and the provisional partials come straight
back down, so the caption tracks the voice at ~400 ms. Finals accumulate into a
pending buffer; the buffer is folded into the deck at the next PHRASE BREAK —
700 ms of quiet after terminal punctuation, longer after a dangling word — and
unconditionally once the buffer is CONTINUOUS_MAX_S old, so a speaker who never
stops still watches the deck move. One Haiku call folds the pending text into
the running deck and the new deck markdown goes down the same socket.

Everything the model can ask for has a compact text form (§ the v3 DSL): slide
visuals are component tags and fenced blocks inside the markdown
(`<Chart>`, `<Meme>`, `<Figure>`, ```mermaid), and everything that is not slide
content is a one-line `@vs <verb> <args>` directive — theme, slide navigation,
feature toggles. Directives are validated here, stripped out of the deck before
it is written, and echoed to the browser as structured frames.

The deck is Slidev markdown, and every pass also writes it to DECK_PATH. That
file is the interface to Slidev proper: `npx slidev <DECK_PATH>` renders the
same deck with hot reload, without this process depending on Node. The Vue
components the deck may reference are mirrored to DECK_PATH's `components/`
alongside it, which is where Slidev auto-imports them from.

Three modalities decide how much licence the formatting pass has: `dictate`
stays near-verbatim, `highlights` condenses and may fetch supporting data with
a tool, `spicy` invents the backdrop. A modality is a prompt plus a tool set;
that is the whole mechanism. Orthogonal to it, a FEATURE SET decides which of
the visual vocabulary the pass is told about at all — turning memes off removes
`<Meme>` from the prompt, not just from the renderer.

`/` is the presenter view: the full captured transcript, the agent-side
conversation, and the controls. `/present.html` is the same deck with nothing
around it, opened in a second window and synced to the presenter over a
BroadcastChannel — no server round trip, so it works with the network unplugged.

There is no HTTP API beyond the two pages. Export is a Blob download in the
browser, so the deck never needs a round trip to leave.

Env:
  BIND PORT DECK_PATH
  SPEECH_URL SPEECH_HOST          heare-speech-services location (Host-routed)
  ANTHROPIC_BASE_URL              inference endpoint; defaults to the ant-proxy
  ANTHROPIC_AUTH_TOKEN            bearer for that endpoint
  MODEL                           Anthropic model id
  PHRASE_PAUSE_S SOFT_PAUSE_S MIDPHRASE_PAUSE_S CONTINUOUS_MAX_S
  CLEANUP_MAX_WORDS
  DEFAULT_MODALITY                dictate | highlights | spicy
  DEFAULT_THEME                   one of THEME_IDS
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

# How long to wait after a final before rewriting the deck. Not one number: how
# an utterance ENDS says whether the speaker finished a thought or paused inside
# one, and waiting the same interval for both either interrupts them mid-clause
# or leaves a finished thought sitting unformatted.
#
#   terminal punctuation  "…so that is the whole mechanism."   -> fire fast
#   soft break            "…there are three of them,"          -> wait a little
#   dangling word         "…and the reason that matters is"     -> wait longer
PHRASE_PAUSE_S = float(os.environ.get("PHRASE_PAUSE_S", "0.7"))
SOFT_PAUSE_S = float(os.environ.get("SOFT_PAUSE_S", "1.2"))
MIDPHRASE_PAUSE_S = float(os.environ.get("MIDPHRASE_PAUSE_S", "1.8"))
# A speaker who never stops never hits a pause of any length, so the pending
# buffer also has a wall-clock ceiling: once the oldest unformatted speech is
# this old, the pass runs regardless of where the sentence is.
CONTINUOUS_MAX_S = float(os.environ.get("CONTINUOUS_MAX_S", "4.0"))
# ...and a size ceiling, for speech that arrives faster than it is formatted.
CLEANUP_MAX_WORDS = int(os.environ.get("CLEANUP_MAX_WORDS", "110"))
# Backwards compatibility: v2's single knob, if someone has it in an env file.
if os.environ.get("CLEANUP_DEBOUNCE_S"):
    MIDPHRASE_PAUSE_S = float(os.environ["CLEANUP_DEBOUNCE_S"])

HERE = pathlib.Path(__file__).parent
STATIC = HERE / "static"
CHEATSHEET = HERE / "slidev-cheatsheet.md"

# One release number, read from the repo's VERSION file. Every place the
# browser needs it -- the service worker's cache name, each page's
# <meta name="app-version">, the ?v= stamps on the asset links, the manifest
# shortcut -- is written as `__VERSION__` on disk and substituted on the way
# out, so bumping that one file is the whole release step and no two files can
# disagree about which version is running.
VERSION_FILE = HERE / "VERSION"
APP_VERSION = (VERSION_FILE.read_text(encoding="utf-8").strip()
               if VERSION_FILE.is_file() else "dev")
VERSION_TOKEN = b"__VERSION__"
SUBSTITUTED = {".html", ".js", ".webmanifest"}

# Entry points answer no-cache. A deploy is invisible until the browser
# re-fetches sw.js and sees different bytes, so sw.js must never be held; the
# pages carry the version meta, and the manifest carries the stamped shortcut.
NO_CACHE = {"index.html", "present.html", "sw.js", "manifest.webmanifest"}
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
- At most one visual (component tag or diagram fence) on a slide, and only when \
the content asks for it. The visuals available this session are listed under \
AVAILABLE THIS SESSION below; that list is exhaustive.
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
- Reach for a chart when the speaker gives you three or more numbers that \
compare, and for a diagram when they describe a flow — if those are listed \
under AVAILABLE THIS SESSION.
- Everything on a slide is either something the speaker said or something you \
fetched this pass. Never a number you remembered.
"""

SPICY = """\
MODALITY: spicy. You are the backdrop of a conference talk given by somebody \
funnier than they realise.

- The speaker's substance stays true: their facts, numbers, names, and claims \
go on the slide unchanged. The garnish is yours.
- Slide titles can be jokes. Bullets can be sassy. A well-placed meme beats a \
paragraph if memes are listed under AVAILABLE THIS SESSION, and \
`layout: statement` beats a bullet list when the line lands.
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


# --------------------------------------------------------------------------
# themes
# --------------------------------------------------------------------------
# A theme is a palette of CSS custom properties, selected by a `data-palette`
# attribute on <html>. The palettes themselves live in static/app.css, because
# that is what has to be true for the popout window to render offline with no
# server in the loop. The IDS live here, because the model has to be able to
# name one, and a name it invents is a theme that silently does nothing.
# test_app.py reads the ids back out of the CSS and fails if the two drift.

@dataclass(frozen=True)
class Theme:
    id: str
    label: str
    blurb: str          # what it looks like, in the words the speaker might use


THEMES: tuple[Theme, ...] = (
    Theme("default", "Default", "indigo on near-white; the neutral one"),
    Theme("parchment", "Parchment", "warm archival paper, oxblood accent, serif headings"),
    Theme("slate", "Slate", "cool grey-blue, restrained, high contrast"),
    Theme("verdigris", "Verdigris", "aged copper green on bone"),
    Theme("brass", "Brass", "warm amber and gold"),
    Theme("noir", "Noir", "monochrome, heavy rules, no colour at all"),
    Theme("solar", "Solar", "high-energy orange and cyan"),
)
THEME_IDS: tuple[str, ...] = tuple(t.id for t in THEMES)

DEFAULT_THEME = os.environ.get("DEFAULT_THEME", "default")
if DEFAULT_THEME not in THEME_IDS:
    DEFAULT_THEME = "default"


# --------------------------------------------------------------------------
# features
# --------------------------------------------------------------------------
# Which of the visual vocabulary is in play. A feature toggle is not a renderer
# switch with a prompt switch bolted on: `capability_block()` builds the prompt
# section from the ENABLED set, so a feature that is off is a feature the model
# was never told exists. Nothing to suppress downstream.

@dataclass(frozen=True)
class Feature:
    id: str
    label: str
    hint: str
    default: bool
    # The lines added to the prompt when this feature is on. Empty for features
    # that change this process's behaviour rather than the model's vocabulary.
    grammar: str = ""


FEATURES: tuple[Feature, ...] = (
    Feature("charts", "Charts", "<Chart> bar and line plots from label/value pairs", True,
            "- A `<Chart>` for three or more numbers that compare. "
            "`<Chart type=\"bar|line\" title=\"…\" unit=\"ms\" "
            ":data=\"[['label', 12], ['label', 34]]\" />`"),
    Feature("memes", "Memes", "<Meme> images from memegen.link", True,
            "- A `<Meme template=\"drake\" top=\"…\" bottom=\"…\" />` when a joke "
            "lands harder as a picture. Templates: drake, fine, two-buttons, "
            "success, doge, grumpycat, rollsafe, disastergirl."),
    Feature("diagrams", "Diagrams", "```mermaid fences rendered as diagrams", True,
            "- A fenced ```mermaid block when the speaker describes a flow, a "
            "sequence, a state machine, or a hierarchy. Keep it under a dozen "
            "nodes; `graph LR` and `sequenceDiagram` read best at slide size."),
    Feature("images", "Images", "<Figure> for a remote image with a caption", True,
            "- A `<Figure src=\"https://…\" caption=\"…\" />` only for an image "
            "URL the speaker gave you or a fetch returned. Never a guessed URL."),
    Feature("themes", "Themes", "the agent may change the deck's palette", True,
            "- `@vs theme <id>` when the speaker asks for a different look."),
    Feature("navigation", "Agent navigation", "the agent may move the presented slide", True,
            "- `@vs goto <n>` / `@vs next` / `@vs prev` / `@vs first` / `@vs last` "
            "when the speaker says which slide to show (\"go back to the "
            "architecture slide\"). Navigation moves the view; it never edits a slide."),
    Feature("incremental", "Live updates", "reformat at phrase breaks while you talk", True),
)
FEATURE_IDS: tuple[str, ...] = tuple(f.id for f in FEATURES)
DEFAULT_FEATURES: frozenset[str] = frozenset(f.id for f in FEATURES if f.default)


# --------------------------------------------------------------------------
# § the v3 DSL — a text representation for everything
# --------------------------------------------------------------------------
# Everything the model can ask the page to do has a short text form, because the
# model pays for every token it emits and the speaker is waiting on the deck. So
# nothing is described in prose and nothing is emitted as markup: a chart is
# thirty characters of label/value pairs, a diagram is a mermaid fence, a theme
# change is four words.
#
# The split is by destination, not by kind. Anything that belongs ON a slide is
# a component tag or a fenced block inside the markdown, and travels in the deck
# because that is where it renders. Anything that is NOT slide content — theme,
# navigation, feature toggles — is a `@vs` line, and is stripped out of the deck
# before it is written, because a deck exported with `@vs next` in it would be a
# deck with a bug in it.

DIRECTIVE_LINE = re.compile(r"(?i)^[ \t]*@vs[ \t]+([a-z]+)[ \t]*([^\n]*?)[ \t]*$")
_FENCE = re.compile(r"^\s*```")


def _int_arg(arg: str, lo: int, hi: int) -> int | None:
    try:
        n = int(arg.strip())
    except (TypeError, ValueError):
        return None
    return n if lo <= n <= hi else None


def _validate_directive(verb: str, arg: str) -> dict[str, Any] | None:
    """One `@vs` line to a structured directive, or None if it is not one.

    Every field is model output derived from a live microphone, so a directive
    is admitted only if it names something that exists: an unknown verb, an
    invented theme id, or a slide number out of range is dropped rather than
    forwarded to the browser to be interpreted there.
    """
    verb, arg = verb.lower(), (arg or "").strip()
    if verb == "theme":
        want = arg.split()[0].lower() if arg else ""
        return {"verb": "theme", "theme": want} if want in THEME_IDS else None
    if verb in ("next", "prev", "first", "last"):
        return {"verb": "nav", "action": verb}
    if verb == "goto":
        n = _int_arg(arg.split()[0] if arg else "", 1, 999)
        return {"verb": "nav", "action": "goto", "index": n} if n else None
    if verb == "feature":
        parts = arg.split()
        if len(parts) != 2 or parts[0].lower() not in FEATURE_IDS:
            return None
        on = parts[1].lower() in ("on", "true", "yes", "enable", "enabled")
        off = parts[1].lower() in ("off", "false", "no", "disable", "disabled")
        if not (on or off):
            return None
        return {"verb": "feature", "feature": parts[0].lower(), "on": on}
    return None


def extract_directives(deck: str) -> tuple[str, list[dict[str, Any]]]:
    """Split a model response into the deck and the directives it carried.

    Fence-aware: a deck that dictates a shell session can legitimately contain
    a line starting `@vs`, and inside a fenced block that line is content.
    """
    out: list[str] = []
    found: list[dict[str, Any]] = []
    fenced = False
    for line in deck.split("\n"):
        if _FENCE.match(line):
            fenced = not fenced
            out.append(line)
            continue
        m = None if fenced else DIRECTIVE_LINE.match(line)
        if not m:
            out.append(line)
            continue
        d = _validate_directive(m.group(1), m.group(2))
        if d is None:
            log.info("dropped unrecognised directive: %r", line.strip())
            continue        # dropped either way: it is not slide content
        found.append(d)
    # A directive on its own line leaves a blank behind it; collapse the runs it
    # creates so removing one does not reshape the deck around it.
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()
    return text, found


# --------------------------------------------------------------------------
# § phrase breaks — when to fold pending speech into the deck
# --------------------------------------------------------------------------

# Terminal punctuation, possibly behind a closing quote or bracket.
TERMINAL = re.compile(r"[.!?\u2026][\"'\u201d\u2019)\]]*$")
# A soft break: the speaker is between clauses but has not landed the thought.
SOFT = re.compile(r"[,;:\u2014-]$")
# Words nobody ends a sentence on. If the utterance stops here, the transcript
# stopped, not the speaker.
DANGLING = re.compile(
    r"(?i)\b(and|but|or|nor|so|because|since|although|though|while|whereas|if|"
    r"unless|until|when|which|that|who|whom|whose|the|a|an|this|these|those|"
    r"my|our|your|their|its|to|of|for|in|on|at|by|with|from|into|about|"
    r"is|are|was|were|be|been|being|has|have|had|will|would|can|could|should|"
    r"very|really|just|about|like)$")


def phrase_break_delay(text: str) -> float:
    """Seconds of quiet to wait after `text` before reformatting the deck.

    The signal is the tail of the utterance. Speech-to-text punctuates well
    enough that the last character is the cheapest available phrase-break
    detector, and a dangling function word catches the case where it does not
    punctuate at all.
    """
    t = (text or "").strip()
    if not t:
        return MIDPHRASE_PAUSE_S
    if TERMINAL.search(t):
        return PHRASE_PAUSE_S
    if SOFT.search(t):
        return SOFT_PAUSE_S
    if DANGLING.search(re.sub(r"[^\w]+$", "", t)):
        return MIDPHRASE_PAUSE_S
    # No punctuation and a content word: a phrase boundary, probably, but the
    # transcript gives no reason to be confident.
    return SOFT_PAUSE_S


def background_note(*, settled: bool, queued_words: int, tools_running: int,
                    passes_done: int) -> str:
    """What to tell the model about work that is still happening around it.

    A pass fired at a phrase break is editing a deck the speaker has not
    finished talking about. Without being told, the model closes the thought off
    — titles the slide, adds a concluding bullet — and the next pass has to undo
    it. Told, it leaves the edge open.
    """
    lines = ["BACKGROUND:"]
    if settled:
        lines.append("- The speaker has stopped. This is a settled pass; the "
                     "dictation below is a complete thought.")
    else:
        lines.append("- The speaker is STILL TALKING. This dictation stops at a "
                     "phrase break, not at the end of the thought. Land what you "
                     "have and leave the edge open: do not add a concluding "
                     "bullet, do not re-title a slide that is still filling up, "
                     "and do not restructure earlier slides this pass.")
    if queued_words:
        lines.append(f"- ~{queued_words} more words are already buffered and will "
                     "arrive in a follow-up pass within a few seconds.")
    if tools_running:
        lines.append(f"- {tools_running} lookup(s) from an earlier pass are still "
                     "in flight; their results may change a slide you are about "
                     "to write. Prefer appending.")
    if not passes_done:
        lines.append("- This is the first pass: the deck is empty, so write the "
                     "headmatter and the cover slide.")
    return "\n".join(lines)


def capability_block(theme: str, features: frozenset[str]) -> str:
    """The prompt section that depends on session state, not on the modality.

    Kept separate from the modality prompt so the cached prefix (the Slidev
    reference, ~2k tokens, identical on every pass) stays cacheable while the
    theme and the feature set move underneath it.
    """
    enabled = [f for f in FEATURES if f.id in features]
    grammar = [f.grammar for f in enabled if f.grammar]
    names = ", ".join(f"`{t.id}` ({t.blurb})" for t in THEMES)
    th = next((t for t in THEMES if t.id == theme), THEMES[0])
    out = [
        "AVAILABLE THIS SESSION. This is the whole vocabulary; anything not "
        "listed here is not available, and a tag or directive you invent "
        "renders as nothing.",
    ]
    out += grammar or ["- Plain Slidev markdown only. No components, no diagrams."]
    if "themes" in features:
        out.append(f"\nThe deck is currently themed `{th.id}` — {th.blurb}. "
                   f"Themes: {names}. Emit `@vs theme <id>` ONLY when the speaker "
                   "asks for a different look (\"switch to the slate theme\", "
                   "\"make it darker\", \"can we get the paper one\"); map what "
                   "they asked for onto the closest id above. A spoken theme "
                   "request is a command, never slide content.")
    out.append(
        "\n`@vs` directives go on their own line, one per line, anywhere in the "
        "response. They are stripped out before the deck is rendered, so they "
        "cost nothing and are never seen by the audience. Everything else you "
        "emit is deck markdown.")
    return "\n".join(out)


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

def _post(mode: Modality, messages: list[dict], with_tools: bool,
          capabilities: str = "") -> dict:
    """One request to the messages API.

    ant-proxy drops the inbound Authorization header and substitutes its own,
    so the bearer sent here is a placeholder. It is real only when
    ANTHROPIC_BASE_URL points somewhere that authenticates its callers.

    Two system blocks, and the order is the point: the cached one holds the
    Slidev reference and the modality prompt, which do not move between passes;
    the uncached one holds the session's theme and feature set, which do. A
    single block would invalidate ~2k tokens of cache every time a toggle moved.
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
        }] + ([{"type": "text", "text": capabilities}] if capabilities else []),
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
                on_tool=None, *, theme: str = DEFAULT_THEME,
                features: frozenset[str] = DEFAULT_FEATURES,
                background: str = "") -> tuple[str, list[dict[str, Any]]]:
    """Blocking Anthropic call. Runs in a thread.

    Returns the new deck markdown and the directives the response carried,
    already validated and already removed from the markdown.

    Modalities without tools settle in one request. A modality with tools may
    round-trip, so the loop runs the tools and hands the results back. The last
    round is always sent WITHOUT tools, which is what guarantees the pass ends
    in a deck rather than in another tool call the budget cannot pay for.
    """
    mode = MODALITIES.get(modality) or MODALITIES[DEFAULT_MODALITY]
    messages: list[dict] = [{"role": "user", "content": (
        f"CURRENT DECK:\n{deck or '(empty — no slides yet)'}\n\n"
        f"NEW DICTATION:\n{dictation}\n\n"
        + (f"{background}\n\n" if background else "")
        + "Return the full updated deck markdown."
    )}]
    caps = capability_block(theme, features)
    out: dict = {}
    for round_ in range(MAX_TOOL_ROUNDS + 1):
        out = _post(mode, messages, with_tools=round_ < MAX_TOOL_ROUNDS,
                    capabilities=caps)
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
    return extract_directives(_strip_fence(text.strip()))


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


def count_slides(deck: str) -> int:
    """Slides in a deck, counted the way the renderer counts them.

    Approximate on purpose: `---` is both the slide separator and the YAML
    fence, and the browser's parser is the one that tells them apart properly.
    This number is for a line in the agent transcript, not for navigation.
    """
    if not deck.strip():
        return 0
    n, fenced = 0, False
    for line in deck.split("\n"):
        if _FENCE.match(line):
            fenced = not fenced
        elif not fenced and line.strip() == "---":
            n += 1
    # Headmatter spends two of those dashes opening the file rather than
    # separating anything, so a deck that starts with one has two fewer slides
    # than its dash count suggests.
    return max(1, n - 1 if deck.lstrip().startswith("---") else n + 1)


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
    """One browser tab: its STT socket, its transcript, its deck, its settings."""

    def __init__(self, ws: ServerConnection):
        self.ws = ws
        self.stt: SttStream | None = None
        self.transcript: list[str] = []
        self.pending: list[str] = []
        self.deck: str = ""
        self.modality: str = DEFAULT_MODALITY
        self.theme: str = DEFAULT_THEME
        self.features: frozenset[str] = DEFAULT_FEATURES
        self._debounce: asyncio.Task | None = None
        self._pass: asyncio.Task | None = None
        self._again = False
        # When the oldest unformatted speech arrived. This, not the gap since
        # the last final, is what the CONTINUOUS_MAX_S ceiling measures: a
        # speaker who pauses for 600 ms every five words resets a debounce
        # forever but does not reset this.
        self._pending_since: float | None = None
        self._settled = True
        self._tools_running = 0
        self._passes = 0

    async def send(self, **msg: Any) -> None:
        with contextlib.suppress(websockets.ConnectionClosed):
            await self.ws.send(json.dumps(msg))

    async def agent(self, role: str, text: str = "", **extra: Any) -> None:
        """One line of the agent-side conversation, for the presenter's pane.

        The presenter view shows what the agent was asked and what it did, next
        to what the speaker actually said. Same socket, separate stream.
        """
        await self.send(type="agent", role=role, text=text, t=time.time(), **extra)

    def queued_words(self) -> int:
        return sum(len(p.split()) for p in self.pending)

    async def push_status(self, text: str) -> None:
        """Status plus the shape of the work behind it.

        The speaker needs to know the difference between "nothing is happening"
        and "a pass is running and two more sentences are already queued behind
        it", which is the same distinction the model is given in `background_note`.
        """
        await self.send(type="status", text=text,
                        busy=bool(self._pass and not self._pass.done()),
                        queued=self.queued_words(),
                        tools=self._tools_running,
                        settled=self._settled)

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
        if self._pending_since is None:
            self._pending_since = time.monotonic()
        await self._schedule_for(text)

    # -- scheduling --------------------------------------------------------

    async def _schedule_for(self, last_final: str) -> None:
        """Pick the moment to reformat, from how the utterance ended.

        Three things can force the pass early, and all three mean the same
        thing: waiting longer would leave the speaker watching a stale deck.
        """
        if "incremental" not in self.features:
            # Live updates off: fold on an explicit flush or end of dictation only.
            return
        delay = phrase_break_delay(last_final)
        self._settled = delay <= PHRASE_PAUSE_S
        age = time.monotonic() - (self._pending_since or time.monotonic())
        if self.queued_words() >= CLEANUP_MAX_WORDS or age + delay >= CONTINUOUS_MAX_S:
            # Mid-sentence by construction — the model is told so.
            delay, self._settled = 0.0, False
        await self._schedule(delay)
        await self.push_status("listening…")

    async def _schedule(self, delay: float) -> None:
        if self._debounce:
            self._debounce.cancel()
        self._debounce = asyncio.create_task(self._after(delay))

    async def _after(self, delay: float) -> None:
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.sleep(delay)
            await self.run_pass()

    async def run_pass(self, settled: bool | None = None) -> None:
        """Fold `pending` into the deck. One pass at a time; a pass requested
        while another is in flight re-runs once the first lands, so speech that
        arrived mid-call is never dropped and never races the deck it edits."""
        if settled is not None:
            self._settled = settled
        if not self.pending:
            return
        if self._pass and not self._pass.done():
            self._again = True
            await self.push_status("formatting…")
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
            self._tools_running += 1 if state == "running" else -1
            asyncio.run_coroutine_threadsafe(
                self.send(type="tool", name=name, detail=detail, state=state, note=note),
                loop)
            asyncio.run_coroutine_threadsafe(
                self.agent("tool", detail or name, tool=name, state=state, note=note),
                loop)

        return report

    async def apply_directives(self, directives: list[dict[str, Any]]) -> None:
        """Act on what the model asked for, and tell both views.

        Already validated by `extract_directives`, so this is dispatch only.
        """
        for d in directives:
            if d["verb"] == "theme":
                self.theme = d["theme"]
                await self.send(type="theme", theme=self.theme, source="agent")
                await self.agent("directive", f"theme → {self.theme}")
            elif d["verb"] == "nav" and "navigation" in self.features:
                await self.send(type="nav", action=d["action"], index=d.get("index"))
                where = f"slide {d['index']}" if d.get("index") else d["action"]
                await self.agent("directive", f"show {where}")
            elif d["verb"] == "feature":
                self.features = (self.features | {d["feature"]}) if d["on"] \
                    else (self.features - {d["feature"]})
                await self.send(type="features", features=sorted(self.features),
                                source="agent")
                await self.agent("directive",
                                 f"{d['feature']} {'on' if d['on'] else 'off'}")

    async def _do_pass(self) -> None:
        dictation = " ".join(self.pending)
        settled = self._settled
        self.pending = []
        self._pending_since = None
        note = background_note(settled=settled, queued_words=0,
                               tools_running=self._tools_running,
                               passes_done=self._passes)
        await self.push_status("formatting…")
        await self.agent("dictation", dictation, settled=settled,
                         words=len(dictation.split()))
        t0 = time.monotonic()
        try:
            deck, directives = await asyncio.to_thread(
                _call_model, self.deck, dictation, self.modality,
                self._tool_reporter(), theme=self.theme, features=self.features,
                background=note)
        except Exception as e:
            # Put the speech back at the front so the next pass still sees it,
            # in the order it was spoken.
            self.pending.insert(0, dictation)
            self._pending_since = time.monotonic()
            log.exception("cleanup pass failed")
            await self.send(type="error", message=f"cleanup pass failed: {type(e).__name__}")
            await self.agent("error", f"{type(e).__name__}: {e}")
            return
        finally:
            ms = int((time.monotonic() - t0) * 1000)
        self._passes += 1
        if deck:
            self.deck = deck
            await asyncio.to_thread(write_deck, deck)
            await self.send(type="deck", markdown=deck, pass_ms=ms, settled=settled)
            await self.agent("deck", f"{count_slides(deck)} slides", ms=ms,
                             bytes=len(deck), settled=settled)
        await self.apply_directives(directives)
        await self.push_status("")
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
                # An explicit "format now" is by definition a settled moment:
                # the speaker stopped and asked.
                await session.run_pass(settled=True)
            elif kind == "deck":
                # The browser owns the deck text once the user edits it.
                session.deck = frame.get("markdown") or ""
            elif kind == "reset":
                session.transcript, session.pending, session.deck = [], [], ""
                session._pending_since, session._passes = None, 0
                await session.send(type="deck", markdown="", pass_ms=0)
            elif kind == "modality":
                wanted = frame.get("id")
                if wanted in MODALITIES:
                    session.modality = wanted
                    await session.push_status(MODALITIES[wanted].label.lower())
                    await session.agent("config", f"modality → {wanted}")
            elif kind == "theme":
                # The picker in the presenter view. Echoed back so the popout,
                # which has no socket of its own, hears about it too.
                wanted = frame.get("id")
                if wanted in THEME_IDS:
                    session.theme = wanted
                    await session.send(type="theme", theme=wanted, source="user")
            elif kind == "feature":
                wanted, on = frame.get("id"), bool(frame.get("on"))
                if wanted in FEATURE_IDS:
                    session.features = (session.features | {wanted}) if on \
                        else (session.features - {wanted})
                    await session.send(type="features",
                                       features=sorted(session.features), source="user")
                    await session.agent("config",
                                        f"{wanted} {'on' if on else 'off'}")
            elif kind == "hello":
                # The vocabulary ships from here — modalities, themes, features —
                # so the controls and the prompts cannot drift apart.
                await session.send(
                    type="config", deck_path=str(DECK_PATH), model=MODEL,
                    version=APP_VERSION,
                    modality=session.modality, theme=session.theme,
                    features=sorted(session.features),
                    phrase_pause_s=PHRASE_PAUSE_S, continuous_max_s=CONTINUOUS_MAX_S,
                    modalities=[{"id": m.id, "label": m.label, "hint": m.hint,
                                 "tools": list(m.tools)} for m in MODALITIES.values()],
                    themes=[{"id": t.id, "label": t.label, "blurb": t.blurb}
                            for t in THEMES],
                    feature_list=[{"id": f.id, "label": f.label, "hint": f.hint}
                                  for f in FEATURES])
    except websockets.ConnectionClosed:
        pass
    finally:
        await session.close()


# --------------------------------------------------------------------------
# static serving — websockets' own HTTP fallback, so there is no second server
# --------------------------------------------------------------------------

def serve_static(connection: ServerConnection, request) -> Response | None:
    # The query comes off first: the asset links and the manifest shortcut
    # carry `?v=<version>`, and a stamped path has to resolve to the same file
    # and the same cache-control as the bare one.
    path = request.path.split("?", 1)[0]
    if path == "/ws":
        return None  # let the handshake proceed
    rel = "index.html" if path in ("/", "") else path.lstrip("/")
    target = (STATIC / rel).resolve()
    if not target.is_file() or STATIC.resolve() not in target.parents:
        return Response(404, "Not Found", Headers({"content-length": "0"}), b"")
    body = target.read_bytes()
    if target.suffix in SUBSTITUTED:
        body = body.replace(VERSION_TOKEN, APP_VERSION.encode())
    ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    if target.suffix in (".js", ".mjs"):
        ctype = "text/javascript"     # mimetypes still says x-javascript on some boxes
    elif target.suffix == ".webmanifest":
        ctype = "application/manifest+json"
    # The vendored mermaid bundle is 2.5 MB and changes when someone re-vendors
    # it, not between requests. Everything past the entry points is safe to
    # hold for an hour because the links to it move with the version.
    if rel in NO_CACHE:
        cache = "no-cache"
    elif "vendor/" in rel:
        cache = "public, max-age=86400"
    else:
        cache = "public, max-age=3600"
    headers = Headers({
        "content-type": ctype,
        "content-length": str(len(body)),
        "cache-control": cache,
        "x-app-version": APP_VERSION,
    })
    if rel == "sw.js":
        # The worker registers with scope './', already its own directory; the
        # header is what keeps that legal if a page ever moves deeper than it.
        headers["service-worker-allowed"] = "/"
    return Response(200, "OK", headers, body)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    sync_components()
    log.info("voice-slides v%s on http://%s:%d  (stt=%s via %s, llm=%s, model=%s, deck=%s, "
             "modality=%s, theme=%s, phrase-break=%.1fs/%.1fs/%.1fs ceiling=%.1fs)",
             APP_VERSION, BIND, PORT, SPEECH_URL, SPEECH_HOST, ANTHROPIC_BASE_URL, MODEL, DECK_PATH,
             DEFAULT_MODALITY, DEFAULT_THEME, PHRASE_PAUSE_S, SOFT_PAUSE_S,
             MIDPHRASE_PAUSE_S, CONTINUOUS_MAX_S)
    async with serve(handler, BIND, PORT, process_request=serve_static, max_size=None):
        await asyncio.Future()


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
