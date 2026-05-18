# Copyright (c) 2025 ByteDance Ltd. and/or its affiliates
# SPDX-License-Identifier: MIT

import unittest

from trae_agent.utils.llm_clients.tool_call_json import parse_tool_call_arguments


class TestParseToolCallArguments(unittest.TestCase):
    def test_none_returns_empty_dict(self):
        result = parse_tool_call_arguments(None)
        self.assertEqual(result, {})

    def test_empty_string_returns_empty_dict(self):
        result = parse_tool_call_arguments("")
        self.assertEqual(result, {})

    def test_whitespace_string_returns_empty_dict(self):
        result = parse_tool_call_arguments("   \n\t  ")
        self.assertEqual(result, {})

    def test_dict_passed_through(self):
        d = {"key": "value"}
        result = parse_tool_call_arguments(d)
        self.assertIs(result, d)
        self.assertEqual(result, {"key": "value"})

    def test_valid_json_object(self):
        result = parse_tool_call_arguments('{"foo": "bar"}')
        self.assertEqual(result, {"foo": "bar"})

    def test_json_with_nested_structure(self):
        result = parse_tool_call_arguments('{"outer": {"inner": [1, 2, 3]}, "flag": true}')
        self.assertEqual(result, {"outer": {"inner": [1, 2, 3]}, "flag": True})

    def test_json_inside_markdown_fence(self):
        result = parse_tool_call_arguments('```json\n{"key": "value"}\n```')
        self.assertEqual(result, {"key": "value"})

    def test_json_inside_markdown_fence_no_lang(self):
        result = parse_tool_call_arguments('```\n{"key": "value"}\n```')
        self.assertEqual(result, {"key": "value"})

    def test_json_with_trailing_text(self):
        result = parse_tool_call_arguments('{"action": "edit"} some trailing commentary')
        self.assertEqual(result, {"action": "edit"})

    def test_json_array_returns_empty_dict(self):
        result = parse_tool_call_arguments("[1, 2, 3]")
        self.assertEqual(result, {})

    def test_invalid_json_returns_empty_dict(self):
        result = parse_tool_call_arguments("not json at all")
        self.assertEqual(result, {})

    def test_malformed_json_returns_empty_dict(self):
        result = parse_tool_call_arguments('{"key": }')
        self.assertEqual(result, {})

    def test_fenced_array_returns_empty_dict(self):
        result = parse_tool_call_arguments("```json\n[1, 2, 3]\n```")
        self.assertEqual(result, {})

    def test_multiple_json_objects_returns_first_dict(self):
        result = parse_tool_call_arguments('[1, 2, 3] {"a": 1} {"b": 2}')
        self.assertEqual(result, {"a": 1})

    def test_nested_braces_in_string_values(self):
        result = parse_tool_call_arguments('{"code": "function foo() { return 1; }"}')
        self.assertEqual(result, {"code": "function foo() { return 1; }"})


if __name__ == "__main__":
    unittest.main()
