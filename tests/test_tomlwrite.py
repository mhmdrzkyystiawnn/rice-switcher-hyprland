"""Tests for the minimal TOML writer (Fase 2, DESIGN-onboarding-tui.md §4.5).

The writer only needs to serialise the manifest value model:
str, int, bool, float, list[str], nested dict, list[dict]. The strongest
check is a round-trip through `tomllib`, both for hand-built manifests and
for the real TOML files under `fixtures/`.
"""

from __future__ import annotations

import glob
import os
import sys
import tomllib
import unittest
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
REPO_ROOT = THIS_DIR.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from rice_switcher.tomlwrite import (
    TomlWriteError,
    dump,
    escape_string,
    sanitize_comment,
)


# At least 15 hand-built manifests covering the real schema.
MANIFESTS = [
    {},
    {"meta": {"name": "x"}},
    {"meta": {"name": "end4", "description": "Baseline desktop",
              "author": "a", "created": "2026-01-01T00:00:00"}},
    {"meta": {"name": "x"}, "wallpaper": "~/wall.png", "config_dir": "."},
    {"meta": {"name": "x"},
     "theme": {"cursor_name": "Bibata", "cursor_size": 24,
               "gtk_theme": "Adwaita", "icon_theme": "Papirus",
               "font_main": "Inter", "font_mono": "JetBrains Mono",
               "font_size": "11", "apply": "gsettings set x y"}},
    {"meta": {"name": "x"}, "hooks": {"pre": ["echo a"],
                                      "post": ["echo b", "echo c"]}},
    {"meta": {"name": "x"}, "run": {"rice_home": "~/rice",
                                    "config_home": "~/.config"}},
    {"meta": {"name": "x"}, "components": [
        {"name": "hyprland", "type": "compositor", "start": "true",
         "stop": "true", "reload": "hyprctl reload",
         "config_link": "~/.config/hypr"},
        {"name": "waybar", "type": "bar", "executable": "waybar",
         "argv_match": ["-c", "config"], "start": "waybar", "stop": "",
         "start_check": "pgrep -x waybar",
         "config_link": "~/.config/waybar", "optional": False, "timeout": 10},
    ]},
    {"meta": {"name": "x"}, "components": [
        {"name": "qs", "type": "shell", "executable": "qs",
         "argv_match": ["-c", "ii"], "start": "qs -c ii", "stop": "",
         "depends_on": ["hyprland"], "timeout": 15}]},
    {"a": {"b": {"c": 1}}},
    {"a": {"b": {"c": 1}, "d": 2}},
    {"empty_table": {}},
    {"empty_list": []},
    {"ints": [1, 2, 3], "bools": [True, False]},
    {"aot": [{"x": 1}, {"x": 2}]},
    {"aot": [{"x": 1, "sub": {"y": 2}}]},
    {"aot": [{"x": 1, "nested": [{"z": 1}]}]},
    {"deep": {"t": {"u": {"v": {"w": 1}}}}},
    {"meta": {"name": "unicode ✓ – 日本語", "description": "emoji 🭖🬥\ttab"}},
    {"weird keys": {"a b": 1, "a.b": 2, "": 3, "$schema": "x"}},
    {"components": [{"name": "a", "argv_match": []},
                    {"name": "b", "argv_match": ["-c", "ii"]}]},
]


class ManifestRoundTripTest(unittest.TestCase):
    def test_at_least_15_manifests(self):
        self.assertGreaterEqual(len(MANIFESTS), 15)

    def test_round_trip(self):
        for m in MANIFESTS:
            text = dump(m)
            parsed = tomllib.loads(text)
            self.assertEqual(parsed, m, f"round-trip failed for {m!r}\n---\n{text}")

    def test_result_is_valid_utf8_str(self):
        for m in MANIFESTS:
            self.assertIsInstance(dump(m), str)


class FixtureRoundTripTest(unittest.TestCase):
    """Every real TOML under fixtures/ must survive a round-trip."""

    def test_fixture_toml_round_trip(self):
        files = sorted(glob.glob(str(REPO_ROOT / "fixtures" / "**" / "*.toml"),
                                 recursive=True))
        self.assertGreaterEqual(len(files), 2)
        for path in files:
            with open(path, "rb") as f:
                original = tomllib.load(f)
            text = dump(original)
            self.assertEqual(tomllib.loads(text), original,
                             f"round-trip failed for {path}")


class EvilStringTest(unittest.TestCase):
    EVIL = [
        'a"b',
        "back\\slash",
        "line\nbreak",
        "tab\there",
        'quote"""triple',
        "unicode ✓ 日 本 🭖",
        "ctrl\x01\x1f\x7f",
        "single'quote",
        "equals = sign",
        "hash # not a comment",
    ]

    def test_evil_strings_round_trip(self):
        for s in self.EVIL:
            d = {"k": s}
            self.assertEqual(tomllib.loads(dump(d)), d, repr(s))

    def test_escape_string_quotes_and_backslash(self):
        self.assertEqual(escape_string('a"b'), '"a\\"b"')
        self.assertEqual(escape_string("a\\b"), '"a\\\\b"')

    def test_escape_string_control_and_newline(self):
        self.assertEqual(escape_string("a\nb"), '"a\\nb"')
        self.assertEqual(escape_string("a\tb"), '"a\\tb"')
        self.assertIn("\\u0001", escape_string("\x01"))
        self.assertIn("\\u007F", escape_string("\x7f"))

    def test_unicode_left_literal(self):
        self.assertIn("✓", escape_string("✓"))
        self.assertNotIn("\\u2713", escape_string("✓"))


class KeyQuotingTest(unittest.TestCase):
    def test_bare_keys_unquoted(self):
        out = dump({"abc-123_X": 1})
        self.assertIn("abc-123_X = 1", out)

    def test_dotted_key_quoted(self):
        out = dump({"a.b": 1})
        self.assertIn('"a.b" = 1', out)

    def test_space_and_empty_key_quoted(self):
        out = dump({"a b": 1, "": 2})
        self.assertIn('"a b" = 1', out)
        self.assertIn('"" = 2', out)

    def test_non_string_key_rejected(self):
        with self.assertRaises(TomlWriteError):
            dump({1: "x"})


class OrderingTest(unittest.TestCase):
    def test_scalars_then_tables_then_aot(self):
        data = {"z_table": {"a": 1}, "a_scalar": 2, "m_aot": [{"x": 1}]}
        out = dump(data)
        self.assertLess(out.index("a_scalar"), out.index("[z_table]"))
        self.assertLess(out.index("[z_table]"), out.index("[[m_aot]]"))

    def test_meta_before_components(self):
        data = {"components": [{"name": "a"}], "meta": {"name": "x"}}
        out = dump(data)
        self.assertLess(out.index("[meta]"), out.index("[[components]]"))


class RaisesTest(unittest.TestCase):
    def test_none_value_raises(self):
        with self.assertRaises(TomlWriteError):
            dump({"k": None})

    def test_unsupported_types_raise(self):
        for bad in ({"k": b"bytes"}, {"k": {1, 2}}, {"k": object()},
                    {"k": [["nested"]]}):
            with self.assertRaises(TomlWriteError):
                dump(bad)

    def test_non_dict_top_level_raises(self):
        with self.assertRaises(TomlWriteError):
            dump([1, 2, 3])


class CommentTest(unittest.TestCase):
    def test_sanitize_strips_newlines_and_controls(self):
        self.assertEqual(sanitize_comment("a\nb"), "a b")
        self.assertEqual(sanitize_comment("a\x01b"), "a b")
        self.assertEqual(sanitize_comment("  x  "), "x")

    def test_scalar_comment_emitted(self):
        out = dump({"meta": {"name": "x"}},
                   comments={"meta.name": "TODO(ask): confirm name"})
        self.assertIn("# TODO(ask): confirm name", out)

    def test_table_comment_emitted(self):
        out = dump({"meta": {"name": "x"}},
                   comments={"meta": "TODO(ask): whole table"})
        self.assertIn("# TODO(ask): whole table", out)

    def test_aot_element_comment_emitted(self):
        out = dump({"components": [{"name": "a"}]},
                   comments={"components.0": "TODO(ask): verify identity"})
        self.assertIn("# TODO(ask): verify identity", out)

    def test_comment_content_cannot_inject_lines(self):
        out = dump({"meta": {"name": "x"}},
                   comments={"meta.name": "line1\nline2\x01extra"})
        joined = out
        self.assertNotIn("line1\nline2", joined)
        self.assertNotIn("\x01", joined)
        for line in out.splitlines():
            self.assertFalse(line.startswith("# line2"))

    def test_header_lines(self):
        out = dump({"a": 1}, header=["generated draft", "review before use"])
        self.assertTrue(out.startswith("# generated draft\n# review before use\n"))


if __name__ == "__main__":
    unittest.main()
