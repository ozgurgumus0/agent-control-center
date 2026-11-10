"""The MCP tools stay read-only and aggregate-only."""
import asyncio
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mcp_server  # noqa: E402

# Field names that would mean a person-level row leaked out.
PERSONAL = ("leadid", "lead_id", "hostid", "host_email", "joinurl", "meetinguuid", "transcript", "phone", "email")


class McpServerTests(unittest.TestCase):
    def test_every_tool_is_marked_read_only(self):
        tools = asyncio.run(mcp_server.server.list_tools())
        self.assertGreaterEqual(len(tools), 14)
        for tool in tools:
            self.assertTrue(tool.annotations.read_only_hint, tool.name)
            self.assertFalse(tool.annotations.destructive_hint, tool.name)

    def test_outputs_carry_no_person_level_fields(self):
        for name in ("overview", "creative_checks", "loi_quality", "loi_calibration", "call_reasons", "incidents", "latest_briefing"):
            text = getattr(mcp_server, name)().lower()
            json.loads(text)                                  # always valid JSON, even when data is missing
            for field in PERSONAL:
                self.assertNotIn(f'"{field}"', text, f"{name} returned {field}")

    def test_instructions_carry_the_no_budget_rule(self):
        self.assertIn("never recommend a budget", mcp_server.INSTRUCTIONS)


if __name__ == "__main__":
    unittest.main()
