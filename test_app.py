#!/usr/bin/env python3
"""Offline tests for the app: modalities, the tool loop, and the fetch guard.

Inference is mocked at `_post`, so nothing here spends a token or touches a
network. The live path is test_e2e.py.

    python3 test_app.py
"""
from __future__ import annotations

import email.message
import json
import os
import re
import pathlib
import tempfile
import time
import unittest

_TMP = tempfile.mkdtemp(prefix="voice-slides-test-")
os.environ["DECK_PATH"] = os.path.join(_TMP, "slides.md")

import app  # noqa: E402  (env has to be set before the module reads it)


class Modalities(unittest.TestCase):
    def test_three_of_them(self):
        self.assertEqual(list(app.MODALITIES), ["dictate", "highlights", "spicy"])

    def test_only_highlights_gets_tools(self):
        self.assertEqual(app.MODALITIES["dictate"].tools, ())
        self.assertEqual(app.MODALITIES["highlights"].tools, ("fetch_url",))
        self.assertEqual(app.MODALITIES["spicy"].tools, ())

    def test_each_prompt_carries_its_own_licence(self):
        prompts = {k: app.build_system_prompt(k) for k in app.MODALITIES}
        self.assertEqual(len(set(prompts.values())), 3, "modality prompts are not distinct")
        self.assertIn("MODALITY: close dictation", prompts["dictate"])
        self.assertIn("fetch_url", prompts["highlights"])
        self.assertIn("MODALITY: spicy", prompts["spicy"])
        for text in prompts.values():
            self.assertIn("Output ONLY the deck markdown", text, "shared contract missing")
            self.assertIn("<slidev-reference>", text)

    def test_an_unknown_modality_falls_back(self):
        self.assertEqual(app.build_system_prompt("nope"), app.build_system_prompt("dictate"))

    def test_the_component_vocabulary_is_in_the_prompt(self):
        # If the components and the cheatsheet drift, the model never emits one.
        prompt = app.build_system_prompt("spicy")
        for token in ("<Chart", "<Meme", ":data=", "memegen"):
            self.assertIn(token, prompt)


class FetchGuard(unittest.TestCase):
    def test_non_public_targets_are_refused(self):
        for url in ("file:///etc/passwd", "ftp://example.com/x", "http://127.0.0.1:8787/v1/messages",
                    "http://localhost/", "http://10.0.0.1/", "http://169.254.169.254/latest/meta-data/",
                    "http://[::1]/", "http://192.168.1.1/"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                app._assert_public(url)

    def test_a_public_target_passes(self):
        app._assert_public("https://8.8.8.8/")     # literal, so no DNS

    def test_html_is_flattened_and_capped(self):
        body = ("<html><head><style>p{color:red}</style><script>evil()</script></head>"
                "<body><h1>Title</h1>" + "<p>filler</p>" * 4000 + "</body></html>").encode()
        headers = email.message.Message()
        headers["content-type"] = "text/html; charset=utf-8"

        class Resp:
            def read(self, n=None): return body[:n]
            def __enter__(self): return self
            def __exit__(self, *a): return False
        Resp.headers = headers

        real, app._OPENER = app._OPENER, type("O", (), {"open": staticmethod(lambda *a, **k: Resp())})
        try:
            out = app.fetch_url("https://8.8.8.8/")
        finally:
            app._OPENER = real
        self.assertNotIn("<p>", out)
        self.assertNotIn("evil()", out, "script body survived the strip")
        self.assertIn("Title", out)
        self.assertLessEqual(len(out), app.FETCH_MAX_CHARS + 20)


class ToolLoop(unittest.TestCase):
    """The loop is mocked at `_post`: no inference, no network."""

    def _fake_post(self, script):
        seen = []

        def post(mode, messages, with_tools, capabilities=""):
            seen.append({"tools": with_tools and bool(mode.tools),
                         "messages": len(messages),
                         "capabilities": capabilities})
            return script.pop(0)
        return post, seen

    def test_a_toolless_modality_settles_in_one_call(self):
        post, seen = self._fake_post([
            {"stop_reason": "end_turn", "content": [{"type": "text", "text": "# Deck"}]}])
        real, app._post = app._post, post
        try:
            out, dirs = app._call_model("", "hello", "dictate")
        finally:
            app._post = real
        self.assertEqual(out, "# Deck")
        self.assertEqual(len(seen), 1)
        self.assertFalse(seen[0]["tools"])

    def test_a_tool_use_is_executed_and_fed_back(self):
        script = [
            {"stop_reason": "tool_use", "content": [
                {"type": "tool_use", "id": "tu_1", "name": "fetch_url",
                 "input": {"url": "https://8.8.8.8/", "why": "checking the figure"}}]},
            {"stop_reason": "end_turn", "content": [{"type": "text", "text": "# Deck\n\n- 42"}]},
        ]
        post, seen = self._fake_post(script)
        reports = []
        real_post, app._post = app._post, post
        real_tools, app.TOOLS = app.TOOLS, {"fetch_url": lambda url, why="": "the answer is 42"}
        try:
            out, dirs = app._call_model("", "how many", "highlights",
                                        lambda *a: reports.append(a))
        finally:
            app._post, app.TOOLS = real_post, real_tools
        self.assertEqual(out, "# Deck\n\n- 42")
        self.assertEqual(len(seen), 2)
        self.assertTrue(seen[0]["tools"])
        self.assertEqual(seen[1]["messages"], 3, "tool result was not fed back")
        # The loading indicator: one running report, one done, carrying `why`.
        self.assertEqual([r[2] for r in reports], ["running", "done"])
        self.assertEqual(reports[0][1], "checking the figure")

    def test_a_failing_tool_costs_a_turn_not_the_deck(self):
        script = [
            {"stop_reason": "tool_use", "content": [
                {"type": "tool_use", "id": "tu_1", "name": "fetch_url",
                 "input": {"url": "http://127.0.0.1/", "why": "nope"}}]},
            {"stop_reason": "end_turn", "content": [{"type": "text", "text": "# Deck"}]},
        ]
        post, _ = self._fake_post(script)
        reports = []
        real, app._post = app._post, post
        try:
            out, dirs = app._call_model("", "x", "highlights", lambda *a: reports.append(a))
        finally:
            app._post = real
        self.assertEqual(out, "# Deck")
        self.assertEqual(reports[-1][2], "error")

    def test_the_last_round_is_always_toolless(self):
        use = {"stop_reason": "tool_use", "content": [
            {"type": "tool_use", "id": "t", "name": "fetch_url",
             "input": {"url": "https://8.8.8.8/", "why": "again"}}]}
        script = [dict(use) for _ in range(app.MAX_TOOL_ROUNDS)] + [
            {"stop_reason": "end_turn", "content": [{"type": "text", "text": "# Deck"}]}]
        post, seen = self._fake_post(script)
        real_post, app._post = app._post, post
        real_tools, app.TOOLS = app.TOOLS, {"fetch_url": lambda url, why="": "data"}
        try:
            out, dirs = app._call_model("", "x", "highlights")
        finally:
            app._post, app.TOOLS = real_post, real_tools
        self.assertEqual(out, "# Deck")
        self.assertEqual(len(seen), app.MAX_TOOL_ROUNDS + 1)
        self.assertFalse(seen[-1]["tools"], "the budget round still offered tools")


class Components(unittest.TestCase):
    def test_they_land_beside_the_deck(self):
        app.write_deck("# hi")
        beside = app.DECK_PATH.parent / "components"
        names = sorted(p.name for p in beside.glob("*.vue"))
        self.assertEqual(names, ["Chart.vue", "Meme.vue"])
        self.assertEqual((beside / "Chart.vue").read_text(),
                         (app.COMPONENTS_SRC / "Chart.vue").read_text())


class PhraseBreaks(unittest.TestCase):
    """When to reformat. The tail of the utterance is the whole signal."""

    def test_terminal_punctuation_fires_fast(self):
        for text in ("So that is the whole mechanism.",
                     "Is that clear?", "Four hundred milliseconds!",
                     'He said "we shipped it."', "…and then it worked…"):
            with self.subTest(text=text):
                self.assertEqual(app.phrase_break_delay(text), app.PHRASE_PAUSE_S)

    def test_a_soft_break_waits_a_little(self):
        for text in ("there are three of them,", "here is the thing:",
                     "two hops, one queue;"):
            with self.subTest(text=text):
                self.assertEqual(app.phrase_break_delay(text), app.SOFT_PAUSE_S)

    def test_a_dangling_word_waits_longer(self):
        for text in ("and the reason that matters is", "we moved it off the",
                     "the problem with that approach and", "it could"):
            with self.subTest(text=text):
                self.assertEqual(app.phrase_break_delay(text), app.MIDPHRASE_PAUSE_S)

    def test_the_tiers_are_ordered(self):
        self.assertLess(app.PHRASE_PAUSE_S, app.SOFT_PAUSE_S)
        self.assertLess(app.SOFT_PAUSE_S, app.MIDPHRASE_PAUSE_S)
        self.assertLess(app.MIDPHRASE_PAUSE_S, app.CONTINUOUS_MAX_S)

    def test_the_fast_tier_is_the_700ms_one(self):
        # The brief's number. If this changes, it changed on purpose.
        self.assertAlmostEqual(app.PHRASE_PAUSE_S, 0.7)

    def test_nothing_said_is_not_a_phrase_break(self):
        self.assertEqual(app.phrase_break_delay(""), app.MIDPHRASE_PAUSE_S)
        self.assertEqual(app.phrase_break_delay(None), app.MIDPHRASE_PAUSE_S)


class Scheduling(unittest.IsolatedAsyncioTestCase):
    """The ceiling, which is what a speaker who never stops actually hits."""

    def _session(self):
        s = app.Session.__new__(app.Session)
        s.ws = None
        s.transcript, s.pending, s.deck = [], [], ""
        s.modality, s.theme = app.DEFAULT_MODALITY, app.DEFAULT_THEME
        s.features = app.DEFAULT_FEATURES
        s._debounce = s._pass = None
        s._again, s._settled, s._tools_running, s._passes = False, True, 0, 0
        s._pending_since = None
        s.scheduled = []
        s.sent = []

        async def send(**m): s.sent.append(m)
        async def schedule(delay): s.scheduled.append(delay)
        s.send, s._schedule = send, schedule
        return s

    async def test_a_mid_sentence_final_schedules_the_long_wait(self):
        s = self._session()
        await s.on_final("and the reason that matters is")
        self.assertEqual(s.scheduled, [app.MIDPHRASE_PAUSE_S])
        self.assertFalse(s._settled)

    async def test_a_finished_sentence_schedules_the_short_wait_and_is_settled(self):
        s = self._session()
        await s.on_final("That is the whole mechanism.")
        self.assertEqual(s.scheduled, [app.PHRASE_PAUSE_S])
        self.assertTrue(s._settled)

    async def test_continuous_speech_hits_the_ceiling_and_fires_immediately(self):
        s = self._session()
        # Pending speech that has already been waiting past the ceiling: no
        # pause of any length is coming, so the pass runs where the sentence is.
        s._pending_since = time.monotonic() - app.CONTINUOUS_MAX_S
        await s.on_final("and then the second thing we tried was")
        self.assertEqual(s.scheduled[-1], 0.0)
        self.assertFalse(s._settled, "a forced pass must be reported as unsettled")

    async def test_the_word_ceiling_also_forces_a_pass(self):
        s = self._session()
        s.pending = ["word"] * (app.CLEANUP_MAX_WORDS + 1)
        await s.on_final("and so on")
        self.assertEqual(s.scheduled[-1], 0.0)

    async def test_live_updates_off_schedules_nothing(self):
        s = self._session()
        s.features = app.DEFAULT_FEATURES - {"incremental"}
        await s.on_final("That is the whole mechanism.")
        self.assertEqual(s.scheduled, [], "a pass was scheduled with live updates off")
        self.assertEqual(s.pending, ["That is the whole mechanism."],
                         "the speech was dropped rather than held for a flush")

    async def test_the_pending_clock_starts_at_the_first_final_not_the_last(self):
        s = self._session()
        await s.on_final("One.")
        first = s._pending_since
        await s.on_final("Two.")
        self.assertEqual(s._pending_since, first)


class BackgroundNote(unittest.TestCase):
    """What the model is told about work happening around it."""

    def test_an_unsettled_pass_says_the_speaker_is_still_talking(self):
        note = app.background_note(settled=False, queued_words=0, tools_running=0,
                                   passes_done=2)
        self.assertIn("STILL TALKING", note)
        self.assertIn("leave the edge open", note)

    def test_a_settled_pass_says_so(self):
        note = app.background_note(settled=True, queued_words=0, tools_running=0,
                                   passes_done=2)
        self.assertIn("has stopped", note)
        self.assertNotIn("STILL TALKING", note)

    def test_queued_speech_and_in_flight_tools_are_both_reported(self):
        note = app.background_note(settled=False, queued_words=34, tools_running=2,
                                   passes_done=1)
        self.assertIn("34 more words", note)
        self.assertIn("2 lookup", note)

    def test_the_first_pass_is_told_the_deck_is_empty(self):
        self.assertIn("first pass", app.background_note(
            settled=True, queued_words=0, tools_running=0, passes_done=0))
        self.assertNotIn("first pass", app.background_note(
            settled=True, queued_words=0, tools_running=0, passes_done=1))

    def test_the_note_reaches_the_model(self):
        seen = {}

        def post(mode, messages, with_tools, capabilities=""):
            seen["user"] = messages[0]["content"]
            seen["caps"] = capabilities
            return {"stop_reason": "end_turn", "content": [{"type": "text", "text": "# D"}]}

        real, app._post = app._post, post
        try:
            app._call_model("", "x", "dictate", background="BACKGROUND:\n- marker")
        finally:
            app._post = real
        self.assertIn("BACKGROUND:", seen["user"])
        self.assertIn("- marker", seen["user"])


class Directives(unittest.TestCase):
    """The `@vs` line: validated here, so the browser interprets nothing."""

    def test_a_theme_directive_is_extracted_and_stripped(self):
        deck, dirs = app.extract_directives("# One\n@vs theme parchment\n\n- a\n")
        self.assertEqual(dirs, [{"verb": "theme", "theme": "parchment"}])
        self.assertNotIn("@vs", deck)
        self.assertIn("# One", deck)
        self.assertIn("- a", deck)

    def test_navigation_verbs(self):
        _, dirs = app.extract_directives(
            "@vs next\n@vs prev\n@vs first\n@vs last\n@vs goto 4\n")
        self.assertEqual([d["action"] for d in dirs],
                         ["next", "prev", "first", "last", "goto"])
        self.assertEqual(dirs[-1]["index"], 4)

    def test_feature_toggles_both_ways(self):
        _, dirs = app.extract_directives("@vs feature memes off\n@vs feature charts on\n")
        self.assertEqual(dirs, [{"verb": "feature", "feature": "memes", "on": False},
                                {"verb": "feature", "feature": "charts", "on": True}])

    def test_an_invented_directive_is_dropped_not_forwarded(self):
        # Every field is model output derived from a live microphone. A theme id
        # that does not exist is a theme that silently does nothing, so it never
        # leaves this process; the line is removed either way, because it is not
        # slide content whatever it says.
        for line in ("@vs theme chartreuse", "@vs goto zero", "@vs goto 0",
                     "@vs goto 1000", "@vs teleport 3", "@vs feature nope on",
                     "@vs feature memes maybe", "@vs theme"):
            with self.subTest(line=line):
                deck, dirs = app.extract_directives(f"# One\n{line}\n")
                self.assertEqual(dirs, [], line)
                self.assertNotIn("@vs", deck, line)

    def test_a_directive_inside_a_fence_is_content(self):
        src = "# One\n\n```sh\n@vs next\n```\n"
        deck, dirs = app.extract_directives(src)
        self.assertEqual(dirs, [])
        self.assertIn("@vs next", deck)

    def test_case_and_leading_whitespace_are_tolerated(self):
        _, dirs = app.extract_directives("  @VS Theme Slate\n")
        self.assertEqual(dirs, [{"verb": "theme", "theme": "slate"}])

    def test_a_deck_with_no_directives_survives_byte_for_byte(self):
        src = "---\ntheme: default\n---\n\n# One\n\n- a\n\n---\n\n# Two"
        self.assertEqual(app.extract_directives(src)[0], src)

    def test_directives_come_back_through_the_model_call(self):
        def post(mode, messages, with_tools, capabilities=""):
            return {"stop_reason": "end_turn", "content": [
                {"type": "text", "text": "# Deck\n@vs theme noir\n"}]}

        real, app._post = app._post, post
        try:
            deck, dirs = app._call_model("", "make it monochrome", "dictate")
        finally:
            app._post = real
        self.assertEqual(deck, "# Deck")
        self.assertEqual(dirs, [{"verb": "theme", "theme": "noir"}])


class ThemesAndFeatures(unittest.TestCase):
    def test_the_palette_ids_match_the_stylesheet(self):
        """The model names a theme; the CSS is what makes that name mean
        something. If they drift, `@vs theme X` validates and does nothing."""
        css = (app.STATIC / "app.css").read_text(encoding="utf-8")
        in_css = set(re.findall(r'\[data-palette="([a-z-]+)"\]', css))
        self.assertEqual(in_css, set(app.THEME_IDS),
                         "app.py THEMES and app.css data-palette blocks disagree")

    def test_every_theme_is_named_in_the_prompt_with_its_blurb(self):
        block = app.capability_block("default", app.DEFAULT_FEATURES)
        for theme in app.THEMES:
            self.assertIn(f"`{theme.id}`", block)
            self.assertIn(theme.blurb, block)

    def test_a_disabled_feature_is_absent_from_the_prompt(self):
        """Not suppressed downstream: never mentioned. A vocabulary the model
        was not given is a vocabulary it cannot emit."""
        full = app.capability_block("default", app.DEFAULT_FEATURES)
        self.assertIn("<Meme", full)
        self.assertIn("mermaid", full)
        without = app.capability_block("default", app.DEFAULT_FEATURES - {"memes", "diagrams"})
        self.assertNotIn("<Meme", without)
        self.assertNotIn("mermaid", without)
        self.assertIn("<Chart", without, "an unrelated feature was dropped too")

    def test_everything_off_still_yields_a_usable_prompt(self):
        block = app.capability_block("default", frozenset())
        self.assertIn("Plain Slidev markdown only", block)
        self.assertNotIn("<Chart", block)

    def test_the_capability_block_is_a_separate_uncached_system_block(self):
        """The Slidev reference is ~2k tokens and identical every pass. Putting
        the session's theme in the same block would invalidate it on a toggle."""
        seen = {}

        def urlopen(req, timeout=None):
            seen["payload"] = json.loads(req.data)

            class R:
                def read(self): return b'{"stop_reason":"end_turn","content":[]}'
                def __enter__(self): return self
                def __exit__(self, *a): return False
            return R()

        real, app.urllib.request.urlopen = app.urllib.request.urlopen, urlopen
        try:
            app._post(app.MODALITIES["dictate"], [{"role": "user", "content": "x"}],
                      False, capabilities="CAPS-MARKER")
        finally:
            app.urllib.request.urlopen = real
        system = seen["payload"]["system"]
        self.assertEqual(len(system), 2)
        self.assertIn("cache_control", system[0])
        self.assertNotIn("cache_control", system[1])
        self.assertEqual(system[1]["text"], "CAPS-MARKER")

    def test_no_credential_is_in_the_payload(self):
        """The proxy holds the credential. Nothing identifying goes up with the
        request either — no metadata, no user id."""
        seen = {}

        def urlopen(req, timeout=None):
            seen["payload"] = json.loads(req.data)
            seen["headers"] = dict(req.headers)

            class R:
                def read(self): return b'{"stop_reason":"end_turn","content":[]}'
                def __enter__(self): return self
                def __exit__(self, *a): return False
            return R()

        real, app.urllib.request.urlopen = app.urllib.request.urlopen, urlopen
        try:
            app._post(app.MODALITIES["dictate"], [{"role": "user", "content": "x"}], False)
        finally:
            app.urllib.request.urlopen = real
        self.assertNotIn("metadata", seen["payload"],
                         "metadata (and so user_id) reached the request")
        auth = seen["headers"].get("Authorization") or ""
        self.assertEqual(auth, f"Bearer {app.ANTHROPIC_AUTH_TOKEN}")
        self.assertFalse(auth.startswith("Bearer sk-"),
                         "a real API key is being sent where the proxy's "
                         "placeholder belongs")
        self.assertNotIn("X-api-key", seen["headers"])

    def test_the_feature_ids_the_renderer_gates_on_are_the_ids_here(self):
        """app.py owns the list; render.js gates on strings. A typo on either
        side is a toggle that does nothing."""
        js = (app.STATIC / "render.js").read_text(encoding="utf-8")
        gated = set(re.findall(r"enabled\('([a-z]+)'\)", js))
        gated |= set(re.findall(r": '([a-z]+)'", js.split("const NEEDS")[1].split("}")[0]))
        self.assertTrue(gated, "found no feature gates in render.js")
        self.assertLessEqual(gated, set(app.FEATURE_IDS),
                             f"render.js gates on ids app.py does not define: "
                             f"{gated - set(app.FEATURE_IDS)}")


class SlideCount(unittest.TestCase):
    def test_headmatter_is_not_a_slide(self):
        self.assertEqual(app.count_slides("---\ntheme: default\n---\n\n# One\n"), 1)
        self.assertEqual(app.count_slides(
            "---\ntheme: default\n---\n\n# One\n\n---\n\n# Two\n"), 2)

    def test_a_deck_without_headmatter_counts_its_separators(self):
        self.assertEqual(app.count_slides("# One\n\n---\n\n# Two\n"), 2)

    def test_an_empty_deck_has_no_slides(self):
        self.assertEqual(app.count_slides(""), 0)
        self.assertEqual(app.count_slides("\n  \n"), 0)

    def test_a_fenced_rule_is_not_a_separator(self):
        self.assertEqual(app.count_slides("# One\n\n```md\n---\n```\n"), 1)


class Views(unittest.TestCase):
    """Both pages exist, load the shared renderer, and hold no inline copy."""

    def test_the_presenter_and_the_popout_share_one_renderer(self):
        for name in ("index.html", "present.html"):
            page = (app.STATIC / name).read_text(encoding="utf-8")
            with self.subTest(page=name):
                self.assertIn("./render.js", page)
                self.assertIn('<script type="module">', page)
                self.assertNotIn("function parseDeck", page,
                                 "the renderer was copied into the page")

    def test_the_popout_needs_no_socket_no_mic_and_no_inference(self):
        page = (app.STATIC / "present.html").read_text(encoding="utf-8")
        # Comments stripped first: the file SAYS it has no socket, and the
        # sentence saying so is not a socket.
        code = re.sub(r"(?m)^\s*//.*$", "", page)
        for forbidden in ("new WebSocket", "getUserMedia", "AudioContext", "fetch("):
            self.assertNotIn(forbidden, code,
                             f"the presentation window reaches for {forbidden}")
        self.assertIn("BroadcastChannel", code)

    def test_mermaid_is_vendored_not_fetched(self):
        vendored = app.STATIC / "vendor" / "mermaid.min.js"
        self.assertTrue(vendored.is_file(), "static/vendor/mermaid.min.js is missing")
        self.assertIn("globalThis.mermaid", vendored.read_text(encoding="utf-8")[-400:])
        js = (app.STATIC / "render.js").read_text(encoding="utf-8")
        self.assertIn("'vendor/mermaid.min.js'", js)
        self.assertNotIn("cdn.", js, "the renderer loads mermaid off a CDN")

    def test_every_control_the_brief_asked_for_is_on_the_presenter_page(self):
        page = (app.STATIC / "index.html").read_text(encoding="utf-8")
        for el in ('id="transcript"',     # the full captured user transcript
                   'id="agent"',          # the agent conversation transcript
                   'id="palettes"',       # theme control
                   'id="features"',       # which features are enabled
                   'id="nav"',            # slide navigation
                   'id="popout"'):        # the presentation window
            self.assertIn(el, page, el)


class Cheatsheet(unittest.TestCase):
    """The prompt's reference section is a document, so it can drift from the
    code. These are the tokens the model has to see to be able to emit them."""

    def setUp(self):
        self.text = app.CHEATSHEET.read_text(encoding="utf-8")

    def test_every_visual_in_the_vocabulary_is_documented(self):
        for token in ("<Chart", "<Meme", "<Figure", "```mermaid"):
            self.assertIn(token, self.text, token)

    def test_every_directive_verb_the_validator_accepts_is_documented(self):
        for verb in ("theme", "goto", "next", "prev", "first", "last", "feature"):
            self.assertIn(f"@vs {verb}", self.text, verb)

    def test_the_directives_the_cheatsheet_teaches_all_validate(self):
        """The reverse drift: an example in the reference that this process
        would drop is an example that teaches the model to emit nothing."""
        examples = re.findall(r"(?m)^@vs .+$", self.text)
        self.assertGreaterEqual(len(examples), 2, "no bare @vs examples found")
        for line in examples:
            with self.subTest(line=line):
                _, dirs = app.extract_directives(line)
                self.assertEqual(len(dirs), 1, f"{line!r} does not validate")


if __name__ == "__main__":
    unittest.main(verbosity=2)
