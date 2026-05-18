# Copyright (c) 2025 ByteDance Ltd. and/or its affiliates
# SPDX-License-Identifier: MIT

import unittest

from trae_agent.utils.lake_view import KNOWN_TAGS, LakeView


class TestKnownTags(unittest.TestCase):
    def test_all_tags_have_emoji(self):
        for tag, emoji in KNOWN_TAGS.items():
            self.assertIsInstance(tag, str)
            self.assertIsInstance(emoji, str)
            self.assertGreater(len(emoji), 0)

    def test_known_tags_include_expected(self):
        expected = {
            "WRITE_TEST",
            "VERIFY_TEST",
            "EXAMINE_CODE",
            "WRITE_FIX",
            "VERIFY_FIX",
            "REPORT",
            "THINK",
            "OUTLIER",
        }
        self.assertEqual(set(KNOWN_TAGS.keys()), expected)


class TestLakeViewGetLabel(unittest.TestCase):
    def setUp(self):
        self.lakeview = LakeView(lake_view_config=None)

    def test_none_tags_returns_empty(self):
        self.assertEqual(self.lakeview.get_label(None), "")

    def test_empty_list_returns_empty(self):
        self.assertEqual(self.lakeview.get_label([]), "")

    def test_single_tag_with_emoji(self):
        result = self.lakeview.get_label(["WRITE_TEST"])
        self.assertIn("WRITE_TEST", result)
        self.assertIn(KNOWN_TAGS["WRITE_TEST"], result)

    def test_single_tag_without_emoji(self):
        result = self.lakeview.get_label(["WRITE_TEST"], emoji=False)
        self.assertEqual(result, "WRITE_TEST")

    def test_multiple_tags_joined_with_separator(self):
        result = self.lakeview.get_label(["EXAMINE_CODE", "WRITE_FIX"])
        parts = result.split(" · ")
        self.assertEqual(len(parts), 2)
        self.assertIn(KNOWN_TAGS["EXAMINE_CODE"], parts[0])
        self.assertIn("EXAMINE_CODE", parts[0])
        self.assertIn(KNOWN_TAGS["WRITE_FIX"], parts[1])
        self.assertIn("WRITE_FIX", parts[1])

    def test_multiple_tags_without_emoji(self):
        result = self.lakeview.get_label(["THINK", "REPORT"], emoji=False)
        self.assertEqual(result, "THINK · REPORT")

    def test_all_known_tags_individually(self):
        for tag in KNOWN_TAGS:
            result = self.lakeview.get_label([tag])
            self.assertIn(tag, result)
            self.assertIn(KNOWN_TAGS[tag], result)

    def test_all_tags_together(self):
        all_tags = list(KNOWN_TAGS.keys())
        result = self.lakeview.get_label(all_tags)
        parts = result.split(" · ")
        self.assertEqual(len(parts), len(all_tags))


if __name__ == "__main__":
    unittest.main()
