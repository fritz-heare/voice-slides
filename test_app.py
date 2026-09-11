#!/usr/bin/env python3
"""Offline tests for the app: modalities, the tool loop, and the fetch guard.

Inference is mocked at `_post`, so nothing here spends a token or touches a
network. The live path is test_e2e.py.

    python3 test_app.py
"""
from __future__ import annotations

import email.message
import os
import pathlib
import tempfile
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

        def post(mode, messages, with_tools):
            seen.append({"tools": with_tools and bool(mode.tools),
                         "messages": len(messages)})
            return script.pop(0)
        return post, seen

    def test_a_toolless_modality_settles_in_one_call(self):
        post, seen = self._fake_post([
            {"stop_reason": "end_turn", "content": [{"type": "text", "text": "# Deck"}]}])
        real, app._post = app._post, post
        try:
            out = app._call_model("", "hello", "dictate")
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
            out = app._call_model("", "how many", "highlights",
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
            out = app._call_model("", "x", "highlights", lambda *a: reports.append(a))
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
            out = app._call_model("", "x", "highlights")
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
