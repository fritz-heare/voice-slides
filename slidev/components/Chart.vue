<!--
  <Chart> — a bar or line chart from a list of [label, value] pairs.

      <Chart type="bar" title="Where the time goes"
             :data="[['encode', 40], ['network', 120], ['decode', 240]]" />

  Hand-rolled SVG rather than a charting library. A library (Chart.js, ECharts)
  would mean the deck directory needs a package.json and an install before
  `npx slidev` renders it, and the deck directory is a file the app writes to
  ~/.local/share — not a project anyone runs `npm i` in. Two chart types drawn
  in ~60 lines of SVG keeps the deck a self-contained artifact.

  The preview in static/index.html draws the same two shapes from the same
  attributes. They are deliberately independent implementations of one
  vocabulary; neither takes a build step, and the deck renders identically
  whether you are looking at the preview or at Slidev.
-->
<script setup>
import { computed } from 'vue'

const props = defineProps({
  type: { type: String, default: 'bar' },     // bar | line
  data: { type: Array, default: () => [] },   // [[label, value], ...]
  title: { type: String, default: '' },
  unit: { type: String, default: '' },
})

const rows = computed(() =>
  props.data
    .map(d => (Array.isArray(d) ? [String(d[0] ?? ''), Number(d[1])] : null))
    .filter(d => d && Number.isFinite(d[1]))
    .slice(0, 12))

const max = computed(() => Math.max(1, ...rows.value.map(r => r[1])))

// Horizontal bars: labels are words, and words read better beside a bar than
// rotated under one.
const BAR_H = 30, LABEL_W = 132, TRACK_X = 140, TRACK_W = 300

const bars = computed(() => rows.value.map(([label, value], i) => ({
  label, value,
  y: i * BAR_H,
  w: Math.max(2, (value / max.value) * TRACK_W),
})))

const LINE_W = 460, LINE_H = 170, PAD = 18

const points = computed(() => rows.value.map(([label, value], i) => ({
  label, value,
  x: PAD + (rows.value.length < 2 ? LINE_W / 2 - PAD : i * (LINE_W - 2 * PAD) / (rows.value.length - 1)),
  y: LINE_H - PAD - (value / max.value) * (LINE_H - 3 * PAD),
})))

const path = computed(() => points.value.map(p => `${p.x},${p.y}`).join(' '))
const fmt = v => (Number.isInteger(v) ? v : v.toFixed(1)) + (props.unit ? ' ' + props.unit : '')
</script>

<template>
  <figure class="vs-chart">
    <figcaption v-if="title">{{ title }}</figcaption>

    <svg v-if="type === 'line'" :viewBox="`0 0 ${LINE_W} ${LINE_H}`" class="plot">
      <line :x1="PAD" :y1="LINE_H - PAD" :x2="LINE_W - PAD" :y2="LINE_H - PAD" class="axis" />
      <polyline :points="path" class="series" />
      <g v-for="p in points" :key="p.label">
        <circle :cx="p.x" :cy="p.y" r="3.5" class="dot" />
        <text :x="p.x" :y="p.y - 9" class="val" text-anchor="middle">{{ fmt(p.value) }}</text>
        <text :x="p.x" :y="LINE_H - 5" class="lbl" text-anchor="middle">{{ p.label }}</text>
      </g>
    </svg>

    <svg v-else :viewBox="`0 0 480 ${Math.max(1, bars.length) * BAR_H}`" class="plot">
      <g v-for="b in bars" :key="b.label" :transform="`translate(0 ${b.y})`">
        <text :x="LABEL_W" y="19" class="lbl" text-anchor="end">{{ b.label }}</text>
        <rect :x="TRACK_X" y="6" :width="TRACK_W" height="14" rx="7" class="track" />
        <rect :x="TRACK_X" y="6" :width="b.w" height="14" rx="7" class="bar" />
        <text :x="TRACK_X + b.w + 8" y="19" class="val">{{ fmt(b.value) }}</text>
      </g>
    </svg>
  </figure>
</template>

<style scoped>
.vs-chart {
  margin: 0.6rem 0;
  padding: 0.9rem 1rem;
  border-radius: 14px;
  background: var(--vs-surface, rgb(127 127 127 / 6%));
  box-shadow: 0 1px 2px rgb(0 0 0 / 6%), 0 8px 24px -12px rgb(0 0 0 / 25%);
}
.vs-chart figcaption {
  font-size: 0.8rem;
  letter-spacing: 0.02em;
  opacity: 0.65;
  margin-bottom: 0.5rem;
}
.plot { width: 100%; height: auto; overflow: visible; }
.bar { fill: var(--vs-accent, #6366f1); }
.track { fill: currentColor; opacity: 0.08; }
.axis { stroke: currentColor; opacity: 0.18; stroke-width: 1; }
.series { fill: none; stroke: var(--vs-accent, #6366f1); stroke-width: 2.5;
          stroke-linejoin: round; stroke-linecap: round; }
.dot { fill: var(--vs-accent, #6366f1); }
.lbl { font-size: 12px; fill: currentColor; opacity: 0.7; }
.val { font-size: 12px; fill: currentColor; opacity: 0.95; font-variant-numeric: tabular-nums; }
</style>
