// voice-slides — the shared renderer.
//
// One module, two views: the presenter (index.html) and the popout
// (present.html) draw the same deck from the same code, and test_render.mjs
// imports this file directly rather than scraping a <script> out of a page.
// That is the whole reason it is a module — before v3 the renderer lived inline
// in the only page there was, and a second view could only have had a copy.
//
// § An APPROXIMATION of Slidev, not Slidev.
//
// The deck is authored as, exported as, and written to disk as valid Slidev
// markdown. Slidev itself renders it (`npx slidev <deck path>`, hot-reloading
// off the same file). What this module does is show the structure while you
// talk: slide boundaries, layouts, headings, bullets, code, presenter notes,
// the component vocabulary, and mermaid diagrams. It does not do Slidev themes,
// UnoCSS, arbitrary Vue components, click animations, or Shiki.
//
// § XSS
//
// Every line is escaped BEFORE any markup is applied, and the only tags that
// reach the DOM are ones this file emits from a closed set. Known Slidev
// wrappers are DELETED (see WRAPPERS), never passed through. Nothing
// transcript-derived is ever interpreted as HTML. The two exceptions are
// explicit and bounded: a component attribute (rebuilt here from parsed parts,
// never interpolated raw) and a mermaid diagram (rendered by mermaid with
// `securityLevel: "strict"`, which sanitizes its own SVG output).

// Which of the visual vocabulary is live. The server owns the real answer and
// pushes it down the socket; this is the renderer's copy of it, so a tag for a
// feature that is off renders as nothing instead of rendering anyway.
let features = new Set(['charts', 'memes', 'diagrams', 'images']);

export function setFeatures(list) { features = new Set(list || []); }
function enabled(id) { return !id || features.has(id); }

export function escapeHtml(s) {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
          .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

// Slidev/Vue wrappers this preview understands. They are DELETED from the
// source before escaping — never re-emitted — so this stays a pure-text path.
const WRAPPERS = /<\/?(?:v-click|v-clicks|v-after|v-switch|v-mark|div|span|p)\b[^>]*>/gi;

export function inline(s) {                       // s is ALREADY escaped
  return s
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[^*])\*([^*]+)\*/g, '$1<em>$2</em>')
    .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1');   // link text only; no href reaches the DOM
}

// -- Slidev document parsing ------------------------------------------------
// `---` on its own line is both the slide separator and the YAML fence, so the
// two are told apart positionally: a block that opens the file, or that opens
// immediately after a separator and closes at the next `---` with nothing but
// `key: value` / indented continuation lines in between, is frontmatter.

// A continuation line inside a block that is already known to be YAML: a
// nested key, a list item, a comment.
const YAMLISH = /^(\s*[\w.$-]+\s*:|[\s-]|#)/;
// What makes a block YAML rather than prose: at least one `key: value`...
const KEYVAL = /^\s*[\w.$-]+\s*:(\s|$)/;
// ...and no heading. `# Title` followed by `- bullet` lines satisfies every
// YAMLISH test (`#` reads as a comment, `-` as a list item), so a slide of
// that shape used to be eaten as frontmatter along with the separator that
// closed it — dropping every other slide from the preview.
const HEADING = /^\s{0,3}#{1,6}\s/;

export function looksLikeFrontmatter(lines, start) {
  let keyval = false;
  for (let i = start; i < lines.length; i++) {
    const l = lines[i];
    if (l.trim() === '---') return keyval;           // closed, and actually YAML
    if (!l.trim()) continue;
    if (HEADING.test(l)) return false;
    if (KEYVAL.test(l)) { keyval = true; continue; }
    if (!YAMLISH.test(l)) return false;
  }
  return false;
}

export function parseYamlish(text) {
  const fm = {};
  for (const l of text.split('\n')) {
    const m = l.match(/^([\w.$-]+)\s*:\s*(.*)$/);
    if (m) fm[m[1]] = m[2].replace(/^['"]|['"]$/g, '').trim();
  }
  return fm;
}

export function parseDeck(md) {
  const lines = md.replace(/\r/g, '').split('\n');
  const slides = [];
  let i = 0, fm = {}, body = [];
  if (lines[0] !== undefined && lines[0].trim() === '---' && looksLikeFrontmatter(lines, 1)) {
    let j = 1;
    while (lines[j].trim() !== '---') j++;
    fm = parseYamlish(lines.slice(1, j).join('\n'));
    i = j + 1;
  }
  const flush = () => {
    if (body.join('').trim() || Object.keys(fm).length) slides.push({ fm, body: body.join('\n') });
    fm = {}; body = [];
  };
  // A fenced code block can contain a line of three dashes; inside one, `---`
  // is content, not a separator.
  let fenced = false;
  for (; i < lines.length; i++) {
    if (/^\s*```/.test(lines[i])) fenced = !fenced;
    if (fenced || lines[i].trim() !== '---') { body.push(lines[i]); continue; }
    flush();
    if (looksLikeFrontmatter(lines, i + 1)) {
      let j = i + 1;
      while (lines[j].trim() !== '---') j++;
      fm = parseYamlish(lines.slice(i + 1, j).join('\n'));
      i = j;
    }
  }
  flush();
  return slides.filter(s => s.body.trim() || s.fm.src);
}

// Trailing HTML comment == presenter note.
export function splitNote(body) {
  const m = body.match(/<!--([\s\S]*?)-->\s*$/);
  return m ? [body.slice(0, m.index), m[1].trim()] : [body, ''];
}

// -- Components -------------------------------------------------------------
// <Chart> and <Meme> are Vue SFCs when Slidev renders the deck (slidev/
// components/, mirrored beside the deck file). Here they are drawn from the
// same attributes by hand, so the preview shows the real shape of the slide
// rather than a placeholder.
//
// Attribute values are model output derived from a live microphone, so nothing
// is evaluated: chart data is scraped for [label, number] pairs by regex, the
// meme template is validated against [a-z0-9-], the image URL is BUILT here
// from a fixed host, and every label is escaped before it reaches the DOM.

const COMPONENT = /^\s*<(Chart|Meme|Figure)\b([^>]*?)\/?>\s*$/;
const ATTR = /(:?[\w-]+)\s*=\s*"([^"]*)"/g;
const PAIR = /\[\s*(?:'([^']*)'|"([^"]*)")\s*,\s*(-?\d+(?:\.\d+)?)\s*\]/g;
const MEME_HOST = 'https://api.memegen.link/images/';

export function attrs(s) {
  const out = {};
  for (const m of s.matchAll(ATTR)) out[m[1].replace(/^:/, '')] = m[2];
  return out;
}

export function pairs(s) {
  const out = [];
  for (const m of (s || '').matchAll(PAIR)) {
    const v = parseFloat(m[3]);
    if (Number.isFinite(v)) out.push([m[1] !== undefined ? m[1] : m[2], v]);
  }
  return out.slice(0, 12);
}

// memegen escapes captions in the URL path with its own scheme, not percent
// encoding. Mirrors seg() in slidev/components/Meme.vue.
export function memeSeg(s) {
  return (String(s || '').trim() || '_')
    .replace(/_/g, '__').replace(/-/g, '--').replace(/ /g, '_')
    .replace(/\?/g, '~q').replace(/&/g, '~a').replace(/%/g, '~p')
    .replace(/#/g, '~h').replace(/\//g, '~s').replace(/\\/g, '~b')
    .replace(/</g, '~l').replace(/>/g, '~g').replace(/"/g, "''")
    .replace(/[^\w~.'-]/g, '');
}

export function renderChart(a) {
  const data = pairs(a.data);
  if (!data.length) return '';
  const unit = a.unit ? ' ' + a.unit : '';
  const fmt = v => escapeHtml((Number.isInteger(v) ? v : v.toFixed(1)) + unit);
  const max = Math.max(1, ...data.map(d => d[1]));
  const cap = a.title ? `<div class="cap">${escapeHtml(a.title)}</div>` : '';
  let svg;
  if (a.type === 'line') {
    const W = 460, H = 170, P = 18;
    const pts = data.map(([l, v], i) => [
      P + (data.length < 2 ? W / 2 - P : i * (W - 2 * P) / (data.length - 1)),
      H - P - (v / max) * (H - 3 * P), l, v]);
    svg = `<svg viewBox="0 0 ${W} ${H}">` +
      `<line class="axis" x1="${P}" y1="${H - P}" x2="${W - P}" y2="${H - P}"></line>` +
      `<polyline class="series" points="${pts.map(p => p[0].toFixed(1) + ',' + p[1].toFixed(1)).join(' ')}"></polyline>` +
      pts.map(([x, y, l, v]) =>
        `<circle class="dot" cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="3.5"></circle>` +
        `<text class="val" x="${x.toFixed(1)}" y="${(y - 9).toFixed(1)}" text-anchor="middle">${fmt(v)}</text>` +
        `<text class="lbl" x="${x.toFixed(1)}" y="${H - 5}" text-anchor="middle">${escapeHtml(l)}</text>`
      ).join('') + '</svg>';
  } else {
    const BH = 30, LW = 132, TX = 140, TW = 300;
    svg = `<svg viewBox="0 0 480 ${data.length * BH}">` + data.map(([l, v], i) => {
      const w = Math.max(2, (v / max) * TW).toFixed(1);
      return `<g transform="translate(0 ${i * BH})">` +
        `<text class="lbl" x="${LW}" y="19" text-anchor="end">${escapeHtml(l)}</text>` +
        `<rect class="track" x="${TX}" y="6" width="${TW}" height="14" rx="7"></rect>` +
        `<rect class="bar" x="${TX}" y="6" width="${w}" height="14" rx="7"></rect>` +
        `<text class="val" x="${(TX + Number(w) + 8).toFixed(1)}" y="19">${fmt(v)}</text></g>`;
    }).join('') + '</svg>';
  }
  return `<figure class="vs-chart">${cap}${svg}</figure>`;
}

export function renderMeme(a) {
  const slug = /^[a-z0-9-]{1,40}$/.test(a.template || '') ? a.template : 'fine';
  const src = MEME_HOST + slug + '/' + memeSeg(a.top) + '/' + memeSeg(a.bottom) + '.png';
  return `<figure class="vs-meme"><img class="meme" src="${escapeHtml(src)}" alt="meme"></figure>`;
}

// <Figure src="https://…" caption="…" /> — the text form of "put that picture
// on the slide". Only https, and the URL is rebuilt from its parsed parts, so a
// `javascript:` or `data:` src cannot survive into the DOM; referrer is
// suppressed because the URL came out of a language model, not out of a person.
export function renderFigure(a) {
  let href = '';
  try {
    const u = new URL(String(a.src || ''));
    if (u.protocol === 'https:') href = u.href;
  } catch (e) { /* not a URL: caption only */ }
  const cap = a.caption ? `<figcaption>${inline(escapeHtml(a.caption))}</figcaption>` : '';
  if (!href) return cap ? `<figure class="vs-figure">${cap}</figure>` : '';
  return `<figure class="vs-figure"><img src="${escapeHtml(href)}" alt="${escapeHtml(a.caption || 'figure')}"` +
         ` loading="lazy" referrerpolicy="no-referrer">${cap}</figure>`;
}

const RENDERERS = { Chart: renderChart, Meme: renderMeme, Figure: renderFigure };
// A visual the session has switched off is not half-rendered: the tag falls
// through to nothing, the same as a tag the renderer does not know.
const NEEDS = { Chart: 'charts', Meme: 'memes', Figure: 'images' };

export function renderComponent(line) {
  const m = line.match(COMPONENT);
  if (!m) return null;
  if (!enabled(NEEDS[m[1]])) return '';
  return RENDERERS[m[1]](attrs(m[2]));
}

// A ```mermaid fence renders as a diagram if mermaid loaded, and as the fence
// text if it did not. Note which way round that is: the container SHIPS with
// the source in it as escaped text, and `hydrateMermaid` replaces that with an
// SVG afterwards. Degradation is the default state rather than an error path,
// so a missing bundle, a blocked CDN, a syntax error in the diagram, and a
// browser with JS half-loaded all land on "you can read the diagram source"
// without any of them being handled.
function closeFence(lang, lines) {
  const body = lines.map(escapeHtml).join('\n');
  if (lang.toLowerCase() === 'mermaid' && enabled('diagrams')) {
    return `<figure class="vs-mermaid" data-mermaid="${escapeHtml(lines.join('\n'))}">` +
           `<pre class="src"><code>${body}</code></pre></figure>`;
  }
  return (lang ? `<div class="lang">${escapeHtml(lang)}</div>` : '') +
         `<pre><code>${body}</code></pre>`;
}

export function renderBody(md) {
  const out = [];
  let list = null, fence = null, lang = '';
  const closeList = () => { if (list) { out.push('</ul>'); list = null; } };
  // A component tag with several attributes gets wrapped over two lines by
  // anything that formats markdown; fold it back to one so the line-oriented
  // loop below sees a whole tag.
  const folded = md.replace(WRAPPERS, '')
                   .replace(/<(?:Chart|Meme|Figure)\b[^>]*>/g, t => t.replace(/\s*\n\s*/g, ' '));
  for (const raw of folded.split('\n')) {
    const line = raw.replace(/\s+$/, '');
    const f = line.match(/^\s*```+\s*([\w+-]*)/);
    if (f) {
      if (fence === null) { closeList(); fence = []; lang = f[1] || ''; }
      else {
        out.push(closeFence(lang, fence));
        fence = null;
      }
      continue;
    }
    if (fence !== null) { fence.push(raw); continue; }
    const comp = renderComponent(line);
    if (comp !== null) { closeList(); if (comp) out.push(comp); continue; }
    if (/^\s*::[\w-]+::\s*$/.test(line)) { closeList(); out.push('<hr class="slot">'); continue; }
    const h = line.match(/^(#{1,4})\s+(.*)$/);
    if (h) { closeList(); const n = h[1].length; out.push(`<h${n}>${inline(escapeHtml(h[2]))}</h${n}>`); continue; }
    const b = line.match(/^(\s*)[-*+]\s+(.*)$/);
    if (b) {
      if (!list) { out.push('<ul>'); list = true; }
      const deep = b[1].length >= 2 ? ' class="sub"' : '';
      out.push(`<li${deep}>${inline(escapeHtml(b[2]))}</li>`);
      continue;
    }
    const q = line.match(/^>\s?(.*)$/);
    if (q) { closeList(); out.push(`<blockquote>${inline(escapeHtml(q[1]))}</blockquote>`); continue; }
    if (!line.trim()) { closeList(); continue; }
    closeList();
    out.push(`<p>${inline(escapeHtml(line))}</p>`);
  }
  if (fence !== null) out.push(closeFence(lang, fence));
  closeList();
  return out.join('');
}


export const CENTERED = new Set(['cover', 'center', 'intro', 'statement', 'fact',
                                 'quote', 'section', 'end']);

// -- § the @vs directive line -----------------------------------------------
// The JS mirror of `extract_directives` in app.py. The server is authoritative
// — it validates, strips, and forwards directives as frames — but the deck can
// also arrive here from a textarea edit or a pasted file, and a `@vs` line that
// reached a slide would be a directive rendered as content. Fence-aware for the
// same reason as the Python side: inside a fence, that line is a shell command.
const DIRECTIVE = /^[ \t]*@vs[ \t]+([a-z]+)[ \t]*(.*?)[ \t]*$/i;

export function splitDirectives(md) {
  const out = [], found = [];
  let fenced = false;
  for (const line of String(md || '').replace(/\r/g, '').split('\n')) {
    if (/^\s*```/.test(line)) { fenced = !fenced; out.push(line); continue; }
    const m = fenced ? null : line.match(DIRECTIVE);
    if (!m) { out.push(line); continue; }
    found.push({ verb: m[1].toLowerCase(), arg: m[2] });
  }
  return { markdown: out.join('\n').replace(/\n{3,}/g, '\n\n').trim(), directives: found };
}

// -- § slides ---------------------------------------------------------------
// The one function both views call. It returns descriptors rather than DOM,
// because the presenter draws every slide as a card in a scroller and the
// popout draws exactly one slide full-bleed, and those have nothing in common
// but the HTML inside them.

export function slides(md) {
  const { markdown } = splitDirectives(md);
  return parseDeck(markdown).map((s, i) => {
    const layout = s.fm.layout || (i === 0 ? 'cover' : 'default');
    const [body, note] = splitNote(s.body);
    return {
      index: i, layout, fm: s.fm,
      centered: CENTERED.has(layout),
      html: renderBody(body),
      note: note ? renderBody(note) : '',
    };
  });
}

// -- § mermaid --------------------------------------------------------------
// Vendored, not fetched: static/vendor/mermaid.min.js is served by this app off
// the same origin, so the popout renders diagrams with the network unplugged.
// Loaded lazily on the first diagram — it is 2.5 MB, and a deck with no
// diagrams in it should not pay for that.

let mermaidReady = null, mermaidSeq = 0;

function loadMermaid() {
  if (mermaidReady) return mermaidReady;
  mermaidReady = new Promise((resolve) => {
    if (globalThis.mermaid) return resolve(globalThis.mermaid);
    const s = document.createElement('script');
    s.src = 'vendor/mermaid.min.js';
    s.onload = () => {
      // strict: mermaid sanitizes its own SVG and refuses click handlers and
      // inline HTML in labels. The diagram source is model output derived from
      // a live microphone; nothing weaker is defensible.
      try { globalThis.mermaid.initialize({ startOnLoad: false, securityLevel: 'strict' }); }
      catch (e) { /* initialize is best-effort; render still works at defaults */ }
      resolve(globalThis.mermaid);
    };
    s.onerror = () => resolve(null);          // the fence text is already on screen
    document.head.appendChild(s);
  });
  return mermaidReady;
}

export async function hydrateMermaid(root) {
  const pending = root ? root.querySelectorAll('.vs-mermaid[data-mermaid]') : [];
  if (!pending.length) return 0;
  const mm = await loadMermaid();
  if (!mm) return 0;
  let done = 0;
  for (const node of pending) {
    const code = node.getAttribute('data-mermaid') || '';
    try {
      const { svg } = await mm.render('vsm' + (++mermaidSeq), code);
      node.innerHTML = svg;
      node.removeAttribute('data-mermaid');
      node.classList.add('ready');
      done++;
    } catch (e) {
      // A diagram the speaker described loosely will not parse. The source is
      // already rendered underneath, which is the useful thing to show.
      node.classList.add('failed');
      node.removeAttribute('data-mermaid');
    }
  }
  return done;
}
