<!--
  <Meme> — an image meme, rendered by api.memegen.link.

      <Meme template="drake" top="polling the API" bottom="a websocket" />

  The component exists for the escaping. memegen encodes caption text in the
  URL PATH with its own scheme (space -> _, _ -> __, - -> --, ? -> ~q, ...),
  which is not percent-encoding and not something to ask a formatting pass to
  get right in the middle of writing a slide. Here the model writes prose in an
  attribute and the escaping is somebody else's problem.

  Templates: https://api.memegen.link/templates/ — drake, distracted-boyfriend,
  fine, doge, success, disastergirl, buzz, philosoraptor, two-buttons, ...
-->
<script setup>
import { computed } from 'vue'

const props = defineProps({
  template: { type: String, default: 'fine' },
  top: { type: String, default: '' },
  bottom: { type: String, default: '' },
  alt: { type: String, default: '' },
})

// memegen's path escaping, from its README.
const seg = s => (String(s).trim() || '_')
  .replace(/_/g, '__').replace(/-/g, '--')
  .replace(/ /g, '_')
  .replace(/\?/g, '~q').replace(/&/g, '~a').replace(/%/g, '~p')
  .replace(/#/g, '~h').replace(/\//g, '~s').replace(/\\/g, '~b')
  .replace(/</g, '~l').replace(/>/g, '~g').replace(/"/g, "''")

// A template id is [a-z0-9-]; anything else is transcription damage, and the
// fallback keeps a broken id from producing a broken URL.
const slug = computed(() => (/^[a-z0-9-]{1,40}$/.test(props.template) ? props.template : 'fine'))

const src = computed(() =>
  `https://api.memegen.link/images/${slug.value}/${seg(props.top)}/${seg(props.bottom)}.png`)
</script>

<template>
  <figure class="vs-meme">
    <img :src="src" :alt="alt || `${top} / ${bottom}`" loading="lazy" />
  </figure>
</template>

<style scoped>
.vs-meme { margin: 0.6rem auto; text-align: center; }
.vs-meme img {
  max-height: 340px;
  border-radius: 14px;
  box-shadow: 0 1px 2px rgb(0 0 0 / 8%), 0 14px 34px -14px rgb(0 0 0 / 45%);
}
</style>
