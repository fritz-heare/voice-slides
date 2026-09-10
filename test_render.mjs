// Renderer tests. Extracts the <script> from static/index.html, stubs the two
// DOM handles the render path touches, and runs it under node — so the code
// under test is the code the browser gets, not a copy.
//
//   node test_render.mjs
import { readFileSync } from 'node:fs';
import assert from 'node:assert';

const html = readFileSync(new URL('./static/index.html', import.meta.url), 'utf8');
const js = html.slice(html.indexOf('<script>') + 8, html.lastIndexOf('</script>'));

// Everything after the renderer touches WebSocket/AudioContext; cut there.
const body = js.slice(0, js.indexOf('// § Transport') - 80);

const el = () => ({ innerHTML: '', children: [], appendChild(c) { this.children.push(c); },
                    addEventListener() {}, scrollIntoView() {}, classList: { add() {} } });
const deckEl = el();
const fakeDoc = { createElement: () => el(), getElementById: () => el(), addEventListener() {} };
const src = body
  .replace(/^"use strict";/, '')
  .replace(/const \$ = [^\n]*\n/, '')
  .replace(/^const statusEl[^\n]*\n/m, '');
const R = new Function('deckEl', 'document',
  `${src}\n  return { parseDeck, renderDeck, renderBody, escapeHtml, splitNote };`
)(deckEl, fakeDoc);

let pass = 0, fail = 0;
const t = (name, fn) => { try { fn(); pass++; console.log('  ok  ' + name); }
                          catch (e) { fail++; console.log('FAIL  ' + name + '\n      ' + e.message); } };

// -- Slidev parsing ---------------------------------------------------------
const DECK = `---
theme: default
title: Latency Budgets
layout: cover
---

# Latency Budgets

How we got to 400ms

---
layout: section
---

# The Problem

---

# Where the time goes

- encode: 40ms
- network: 120ms
- decode: 240ms

<!--
Mention the p99 here.
-->
`;

t('headmatter is not a slide', () => {
  const s = R.parseDeck(DECK);
  assert.strictEqual(s.length, 3, 'got ' + s.length + ' slides');
});

t('headmatter attaches to slide 1', () => {
  const s = R.parseDeck(DECK);
  assert.strictEqual(s[0].fm.layout, 'cover');
  assert.strictEqual(s[0].fm.title, 'Latency Budgets');
});

t('per-slide frontmatter is parsed, not rendered', () => {
  const s = R.parseDeck(DECK);
  assert.strictEqual(s[1].fm.layout, 'section');
  assert.ok(!s[1].body.includes('layout:'), 'frontmatter leaked into body');
});

t('trailing comment becomes a presenter note', () => {
  const s = R.parseDeck(DECK);
  const [b, note] = R.splitNote(s[2].body);
  assert.strictEqual(note, 'Mention the p99 here.');
  assert.ok(!b.includes('<!--'));
});

t('a deck with no headmatter still parses', () => {
  const s = R.parseDeck('# One\n\n---\n\n# Two\n');
  assert.strictEqual(s.length, 2);
});

t('a fenced yaml block does not split the deck', () => {
  const s = R.parseDeck('# One\n\n```yaml\nfoo: 1\n```\n\n---\n\n# Two\n');
  assert.strictEqual(s.length, 2);
});

t('a literal --- inside a fence does not split the deck', () => {
  const s = R.parseDeck('# One\n\n```md\na\n---\nb\n```\n\n---\n\n# Two\n');
  assert.strictEqual(s.length, 2, 'got ' + s.length);
});

// -- rendering --------------------------------------------------------------
t('bullets and headings render', () => {
  const h = R.renderBody('# T\n\n- a\n- b\n');
  assert.ok(h.includes('<h1>T</h1>') && h.includes('<li>a</li>'), h);
});

t('code fence renders as escaped <pre>', () => {
  const h = R.renderBody('```js\nif (a < b) {}\n```');
  assert.ok(h.includes('<pre><code>') && h.includes('a &lt; b'), h);
});

t('v-click wrappers are stripped, content kept', () => {
  const h = R.renderBody('<v-clicks>\n\n- one\n- two\n\n</v-clicks>');
  assert.ok(h.includes('<li>one</li>') && !h.includes('v-clicks'), h);
});

// -- XSS: transcript-derived content is never markup ------------------------
const HOSTILE = [
  '<img src=x onerror="alert(1)">',
  '<script>alert(1)</script>',
  '<a href="javascript:alert(1)">click</a>',
  '[click](javascript:alert(1))',
  '<div onmouseover=alert(1)>hi</div>',
  '<svg/onload=alert(1)>',
  '"><script>alert(1)</script>',
  '`<img src=x onerror=alert(1)>`',
];

// The real invariant is not "the string looks harmless" — escaped text like
// `&lt;img onerror=&quot;...&quot;&gt;` trips every naive substring check while
// being inert. It is: the ONLY tags in the output are ones the renderer itself
// emits, and none of them carry an attribute.
const ALLOWED = new Set(['h1','h2','h3','h4','ul','li','p','code','pre','strong','em','blockquote','div','hr']);

t('hostile transcript emits no tag outside the renderer allowlist', () => {
  for (const s of HOSTILE) {
    const out = R.renderBody('- ' + s);
    for (const m of out.matchAll(/<\/?([a-zA-Z][\w-]*)([^>]*)>/g)) {
      assert.ok(ALLOWED.has(m[1].toLowerCase()), `tag <${m[1]}> escaped sanitization: ${s} -> ${out}`);
      const attrs = m[2].replace(/\s*\/$/, '').trim();
      assert.ok(attrs === '' || /^class="(sub|slot|lang)"$/.test(attrs),
                `unexpected attribute on <${m[1]}>: ${attrs} (from ${s})`);
    }
  }
});

t('markdown link hrefs never reach the DOM', () => {
  const out = R.renderBody('- [click](javascript:alert(1))');
  assert.ok(!out.includes('href'), out);
  assert.ok(out.includes('click'), out);
});

t('hostile content survives as visible TEXT', () => {
  const out = R.renderBody('- <img src=x onerror=alert(1)>');
  assert.ok(out.includes('&lt;img'), 'text was dropped instead of escaped: ' + out);
});

t('hostile frontmatter values are escaped in the layout badge', () => {
  const fm = R.parseDeck('---\nlayout: "<img src=x onerror=alert(1)>"\n---\n\n# hi\n')[0].fm;
  assert.ok(!R.escapeHtml(fm.layout).includes('<img'));
});

// -- frontmatter detection --------------------------------------------------
// A `# Heading` + `- bullets` block satisfies the old YAMLISH test on every
// line (`#` reads as a comment, `-` as a list item), so it was consumed as
// frontmatter along with the separator that closed it — and every slide of
// that shape vanished from the preview.
t('a heading + bullets block after a separator is a slide, not frontmatter', () => {
  const s = R.parseDeck(`---
theme: default
title: T
layout: cover
---

# T

sub

---

# Slide Two

- a
- b

---

# Slide Three

- c
`);
  assert.strictEqual(s.length, 3, 'got ' + s.length + ' slides: ' + JSON.stringify(s.map(x => x.body.trim().slice(0, 14))));
  assert.ok(s[1].body.includes('Slide Two'), 'slide two was eaten');
  assert.ok(s[2].body.includes('Slide Three'));
});

t('a bullets-only block after a separator is not frontmatter', () => {
  const s = R.parseDeck('# One\n\n---\n\n- a\n- b\n\n---\n\n# Three\n');
  assert.strictEqual(s.length, 3, 'got ' + s.length);
});

t('frontmatter still wins when it is actually frontmatter', () => {
  const s = R.parseDeck('# One\n\n---\nlayout: two-cols\nclass: text-sm\n---\n\n# Two\n');
  assert.strictEqual(s.length, 2, 'got ' + s.length);
  assert.strictEqual(s[1].fm.layout, 'two-cols');
  assert.ok(!s[1].body.includes('layout:'), 'frontmatter leaked into the body');
});

// -- components -------------------------------------------------------------
t('<Chart> renders one bar per pair', () => {
  const h = R.renderBody(`<Chart type="bar" title="Time" unit="ms" :data="[['encode', 40], ['network', 120], ['decode', 240]]" />`);
  assert.strictEqual((h.match(/class="bar"/g) || []).length, 3, h);
  assert.ok(h.includes('>encode<') && h.includes('>240 ms<'), h);
});

t('<Chart type="line"> renders a polyline and a dot per point', () => {
  const h = R.renderBody(`<Chart type="line" :data="[['w1', 820], ['w2', 610], ['w3', 400]]" />`);
  assert.ok(h.includes('<polyline'), h);
  assert.strictEqual((h.match(/class="dot"/g) || []).length, 3, h);
});

t('a component tag wrapped over two lines still renders', () => {
  const h = R.renderBody(`<Chart type="bar" title="Where the time goes"\n       :data="[['a', 1], ['b', 2]]" />`);
  assert.ok(h.includes('class="vs-chart"') && h.includes('>b<'), h);
  assert.ok(!h.includes('&lt;Chart'), 'tag leaked through as text: ' + h);
});

t('<Chart> with no parseable data renders nothing', () => {
  assert.strictEqual(R.renderBody('<Chart type="bar" :data="whatever" />'), '');
});

t('<Meme> builds a memegen url with memegen escaping', () => {
  const h = R.renderBody('<Meme template="drake" top="polling the API" bottom="a websocket" />');
  assert.ok(h.includes('src="https://api.memegen.link/images/drake/polling_the_API/a_websocket.png"'), h);
});

t('<Meme> falls back when the template id is not a template id', () => {
  const h = R.renderBody('<Meme template="../../etc/passwd" top="no" bottom="no" />');
  assert.ok(h.includes('/images/fine/'), h);
  assert.ok(!h.includes('passwd'), h);
});

// The component path is the one place the renderer emits an attribute whose
// value is derived from the transcript, so it gets its own hostile pass.
t('hostile component attributes cannot break out of the attribute', () => {
  const hostile = [
    `<Meme template="x\\" onerror=\\"alert(1)" top="a" bottom="b" />`,
    `<Meme template="fine" top="\\" onerror=\\"alert(1)" bottom="b" />`,
    `<Chart type="bar" title="<img src=x onerror=alert(1)>" :data="[['<svg onload=alert(1)>', 1]]" />`,
    `<Chart type="bar" :data="[['a', 1]]" onload="alert(1)" />`,
  ];
  // A tag whose attribute value contains a `>` never matches the component
  // pattern at all and falls through to escaped text, which is inert — so the
  // invariant is about the tags the renderer EMITS, not about substrings.
  const OK = new Set(['figure', 'div', 'svg', 'g', 'rect', 'line', 'text', 'circle',
                      'polyline', 'img', 'p']);
  const OK_ATTR = /^(class|x|y|x1|y1|x2|y2|cx|cy|r|rx|width|height|viewBox|points|transform|text-anchor|src|alt|loading)$/;
  for (const s of hostile) {
    const out = R.renderBody(s);
    for (const m of out.matchAll(/<\/?([a-zA-Z][\w-]*)([^>]*)>/g)) {
      assert.ok(OK.has(m[1].toLowerCase()), `tag <${m[1]}> from ${s}`);
      for (const a of m[2].matchAll(/([\w-]+)\s*=/g)) {
        assert.ok(OK_ATTR.test(a[1]), `attribute ${a[1]} on <${m[1]}> from ${s}: ${out}`);
      }
    }
    for (const m of out.matchAll(/src="([^"]*)"/g)) {
      assert.ok(m[1].startsWith('https://api.memegen.link/images/'),
                'src outside the meme host: ' + m[1]);
    }
  }
});

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
