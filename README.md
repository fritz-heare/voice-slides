# voice-slides

Dictate a [Slidev](https://sli.dev) deck. An agent formats it while you talk.

One page, one Python file, one dependency. Speech goes to
heare-speech-services' streaming STT on pook; every couple of sentences a
Claude Haiku pass folds what you said into the running deck; the preview
re-renders and follows the dictation head.

```
browser mic ──16kHz PCM16──▶ app.py ──▶ /stt/stream (pook)   partials ~400ms
                               │◀─────────────────────────── finals
                               │
                               ├─ debounce ─▶ Haiku ─▶ Slidev markdown
                               │
                               ├─▶ browser (live preview)
                               └─▶ ~/.local/share/voice-slides/slides.md
```

## Run it

```bash
uv run app.py                  # http://127.0.0.1:8097
```

`uv` reads the PEP-723 header and installs the one dependency. Plain
`python3 app.py` also works wherever `websockets>=14` is already present.

Then open the page, press **Start dictating**, and talk. Pauses end an
utterance; a couple of seconds of quiet triggers the formatting pass.

| control | does |
|---|---|
| Start dictating / Space | toggle the mic |
| Format now | run the pass immediately instead of waiting for the pause |
| Export slides.md | download the deck |
| Open in Slidev | copy `npx slidev <deck path>` |
| click a slide | pin the preview there; click again to unpin |

## The deck file

Every formatting pass writes the deck to `DECK_PATH`
(`~/.local/share/voice-slides/slides.md` by default), atomically. That file is
the seam between this app and Slidev proper:

```bash
npx slidev ~/.local/share/voice-slides/slides.md
```

Slidev's dev server watches the file and hot-reloads, so you get the real
renderer — themes, Shiki, click animations, presenter mode — in a second window
while you keep dictating into this one. voice-slides itself never needs Node.

## Preview vs. Slidev

The built-in preview is an approximation: slide boundaries, layouts, headings,
bullets, code, presenter notes. It does not do themes, UnoCSS, Vue components,
click animations, or syntax highlighting. It exists so you can see the deck's
*shape* moving while you talk, at zero dependency cost. When you want the real
thing, point Slidev at the file.

## Config

| env | default | |
|---|---|---|
| `BIND` / `PORT` | `127.0.0.1` / `8097` | |
| `DECK_PATH` | `~/.local/share/voice-slides/slides.md` | mirrored every pass |
| `SPEECH_URL` | `http://pook.tail5ae4b.ts.net` | |
| `SPEECH_HOST` | `heare-speech-services` | Host-header vhost; never use a raw port |
| `CRED_URL` | `http://localhost:9876/api/credentials` | |
| `ANTHROPIC_CRED_ID` | `claude-subscription.seanfitz` | billed to the Max subscription, not API credits |
| `MODEL` | `claude-haiku-4-5-20251001` | |
| `CLEANUP_DEBOUNCE_S` | `2.5` | silence before a pass |
| `CLEANUP_MAX_WORDS` | `110` | pass anyway if the buffer gets this big |

The Anthropic token is read from the credential store at call time and lives
only in the request header. It is never logged, cached, or written to disk.

## The formatting prompt

`slidev-cheatsheet.md` is a condensed Slidev syntax reference (separators,
headmatter, per-slide frontmatter, layouts, notes, code blocks, click
animations). It is loaded verbatim into the system prompt on every pass, and
cached, so it is editable as a document rather than as a string literal.

`INSTRUCTIONS` in `app.py` is the rest: deck conventions, filler-stripping,
and the spoken structure commands ("new slide", "title this X", "scratch that",
"add a note") the model must obey rather than transcribe.

## Tests

```bash
node test_render.mjs    # Slidev parsing + XSS posture; no network
python3 test_e2e.py     # real pook STT + real Haiku; needs the tailnet
```

`test_render.mjs` extracts the `<script>` from `static/index.html` and runs it
under node, so the code under test is the code the browser gets.

## Install as a service

Not enabled by default.

```bash
cp voice-slides.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now voice-slides
```
