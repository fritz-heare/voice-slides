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

The formatting pass calls the ant-proxy on `localhost:8787`, so that service
must be up (`systemctl --user status ant-proxy`). Nothing else is needed for
auth.

Then open the page, press **Start dictating**, and talk. Pauses end an
utterance; a couple of seconds of quiet triggers the formatting pass.

| control | does |
|---|---|
| Start dictating / Space | toggle the mic |
| modality picker | how much licence the formatting pass has — see below |
| ◐ | light / dark / follow the system |
| Format now | run the pass immediately instead of waiting for the pause |
| Export slides.md | download the deck |
| Open in Slidev | copy `npx slidev <deck path>` |
| click a slide | pin the preview there; click again to unpin |

## Modalities

The picker in the header chooses what the formatting pass is allowed to do
with what you said. A modality is a system prompt plus a tool set; there is no
other mechanism.

| modality | the pass | tools |
|---|---|---|
| **Close dictation** | near-verbatim — your sentences, your order, filler and false starts removed | none |
| **Highlights + sources** | condensed to key points, and it fetches data to back them up | `fetch_url` |
| **Spicy** | your facts, a backdrop with opinions: joke titles, sassy bullets, the odd meme | none |

In **highlights**, a claim that rests on something checkable sends the model to
`fetch_url` — a plain GET, capped at two fetches a pass, restricted to public
http(s) addresses (the resolved IP is checked, and every redirect hop is
re-checked, so a name that answers `127.0.0.1` is refused). A fetch in flight
shows as a chip in the deck pane with what it is checking, so a ten-second pass
reads as work rather than as a hang. A fetch that fails costs the model a turn,
not your deck update.

The default is close dictation; `DEFAULT_MODALITY` changes it, and the picker
remembers your last choice.

## Components

The deck can use two components beyond stock Slidev markdown, and the model
knows about both — they are documented in `slidev-cheatsheet.md`, which is the
system prompt's reference section.

```md
<Chart type="bar" title="Where the time goes" unit="ms"
       :data="[['encode', 40], ['network', 120], ['decode', 240]]" />

<Meme template="drake" top="polling the API" bottom="a websocket" />
```

`<Chart>` does bar and line from `[label, value]` pairs, drawn as plain SVG —
no charting library, so the deck directory stays a self-contained artifact you
can hand to somebody rather than a project someone has to `npm i`. `<Meme>`
exists for the escaping: memegen encodes captions in the URL path with its own
scheme, which is not a thing to ask a formatting pass to get right mid-slide.
For flowcharts, a `mermaid` fence works — Slidev renders those natively.

They live in `slidev/components/` and every deck write mirrors them into
`components/` beside `DECK_PATH`, which is where Slidev auto-imports from. The
live preview draws the same two shapes from the same attributes.

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
bullets, code, presenter notes, and the two bundled components. It does not do
themes, UnoCSS, arbitrary Vue components, click animations, mermaid, or syntax
highlighting. It exists so you can see the deck's
*shape* moving while you talk, at zero dependency cost. When you want the real
thing, point Slidev at the file.

## Config

| env | default | |
|---|---|---|
| `BIND` / `PORT` | `127.0.0.1` / `8097` | |
| `DECK_PATH` | `~/.local/share/voice-slides/slides.md` | mirrored every pass |
| `SPEECH_URL` | `http://pook.tail5ae4b.ts.net` | |
| `SPEECH_HOST` | `heare-speech-services` | Host-header vhost; never use a raw port |
| `ANTHROPIC_BASE_URL` | `http://localhost:8787` | the ant-proxy |
| `ANTHROPIC_AUTH_TOKEN` | `proxied` | placeholder; the proxy substitutes its own bearer |
| `MODEL` | `claude-haiku-4-5-20251001` | |
| `DEFAULT_MODALITY` | `dictate` | `dictate` \| `highlights` \| `spicy` |
| `CLEANUP_DEBOUNCE_S` | `2.5` | silence before a pass |
| `CLEANUP_MAX_WORDS` | `110` | pass anyway if the buffer gets this big |

Inference goes through ant-proxy on `localhost:8787`, which holds the
credential, refreshes it, and stamps the `Authorization` header on the way to
`api.anthropic.com`. This app reads no credential and holds no token. Point
`ANTHROPIC_BASE_URL` elsewhere and `ANTHROPIC_AUTH_TOKEN` becomes the real
bearer.

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
node test_render.mjs    # Slidev parsing, components, XSS posture; no network
python3 test_app.py     # modalities, the tool loop, the fetch guard; inference mocked
python3 test_e2e.py     # real pook STT + real Haiku via ant-proxy; needs the tailnet
```

`test_render.mjs` extracts the `<script>` from `static/index.html` and runs it
under node, so the code under test is the code the browser gets. `test_app.py`
mocks inference at `_post`, so it spends nothing and needs no network.

## Install as a service

Not enabled by default.

```bash
cp voice-slides.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now voice-slides
```
