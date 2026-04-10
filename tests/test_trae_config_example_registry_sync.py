# Copyright (c) 2025 ByteDance Ltd. and/or its affiliates
# SPDX-License-Identifier: MIT

"""Keep ``trae_config.yaml.example`` in sync with ``trae_agent.tools.tools_registry``."""

import re
import unittest
from pathlib import Path

from trae_agent.tools import tools_registry

REPO_ROOT = Path(__file__).resolve().parents[1]
TRAE_CONFIG_EXAMPLE = REPO_ROOT / "trae_config.yaml.example"


class TestTraeConfigExampleRegistrySync(unittest.TestCase):
    def test_legal_id_line_lists_exactly_tools_registry(self):
        text = TRAE_CONFIG_EXAMPLE.read_text(encoding="utf-8")
        m = re.search(r"^#\s*合法 id[：:]\s*([^\n]+)\s*$", text, re.MULTILINE)
        if m is None:
            self.fail(
                f"{TRAE_CONFIG_EXAMPLE.name} must contain a comment line "
                "'# 合法 id：...' listing all builtin tool ids"
            )
        raw = m.group(1)
        listed = {x.strip() for x in re.split(r"[,，]\s*", raw) if x.strip()}
        expected = set(tools_registry.keys())
        self.assertEqual(
            listed,
            expected,
            f"Update the '合法 id' line in {TRAE_CONFIG_EXAMPLE.name} to match "
            f"tools_registry keys: {sorted(expected)}",
        )

    def test_example_tools_block_includes_all_registry_tools(self):
        """Commented whitelist example should list every registry key (same set as 合法 id)."""
        text = TRAE_CONFIG_EXAMPLE.read_text(encoding="utf-8")
        in_tools_example = False
        dashed: set[str] = set()
        for line in text.splitlines():
            if "示例" in line and "白名单" in line and line.strip().startswith("#"):
                in_tools_example = True
                continue
            if not in_tools_example:
                continue
            # End of commented block: first non-comment line at column 0 that's not empty
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                break
            mm = re.match(r"^\s*#\s+-\s+(\S+)\s*$", line)
            if mm:
                dashed.add(mm.group(1))
        expected = set(tools_registry.keys())
        self.assertEqual(
            dashed,
            expected,
            f"Commented '#     tools:' example in {TRAE_CONFIG_EXAMPLE.name} should "
            f"include one '- name' line per registry tool: {sorted(expected)}",
        )


if __name__ == "__main__":
    unittest.main()
