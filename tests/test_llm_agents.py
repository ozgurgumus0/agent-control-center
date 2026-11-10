"""Offline tests for llm_agents: the tools, the guards and a full run on a fake client.

No test here reaches the Anthropic API. Tests that need local data (the
allocator extract, a real incident folder) are skipped when it is absent; set
ALLOCATOR_DATA / ATTRIBUTION_PROJECT_ROOT and TEST_INCIDENT_ID to run them.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import llm_agents  # noqa: E402

INCIDENT = os.getenv("TEST_INCIDENT_ID", "example-metric-anomaly-spend-eur-google")
HAS_INCIDENT = llm_agents._incident_dir(INCIDENT) is not None
HAS_EXTRACT = llm_agents.ALLOCATOR_EXTRACT.is_file()
needs_incident = unittest.skipUnless(HAS_INCIDENT, "no local incident folder (TEST_INCIDENT_ID)")
needs_extract = unittest.skipUnless(HAS_EXTRACT, "no local allocator extract (ALLOCATOR_DATA)")


class FakeRunner:
    """Plays a scripted conversation: each step is a list of (tool, input) calls."""

    def __init__(self, tools, script, usage):
        # Server tools (web search) are plain dicts with no function to call.
        self.tools = {tool.name: tool for tool in tools if not isinstance(tool, dict)}
        self.script, self.usage, self.results = script, usage, []

    def __iter__(self):
        for calls in self.script:
            for name, arguments in calls:
                self.results.append(self.tools[name].call(arguments))
            yield SimpleNamespace(usage=self.usage, stop_reason="tool_use", content=[])

    def generate_tool_call_response(self):
        return None


class FakeClient:
    def __init__(self, script, usage):
        self.script, self.usage, self.kwargs = script, usage, None
        self.beta = SimpleNamespace(messages=SimpleNamespace(tool_runner=self._runner))

    def _runner(self, **kwargs):
        self.kwargs = kwargs
        self.runner = FakeRunner(kwargs["tools"], self.script, self.usage)
        return self.runner


USAGE = SimpleNamespace(input_tokens=10_000, output_tokens=1_000,
                        cache_read_input_tokens=0, cache_creation_input_tokens=0)

REPORT = {"headline": "h", "findings": [{"title": "t", "story": "s", "key_number": "k"}] * 3,
          "watch_items": ["Meta CTR"], "follow_up": [], "appendix_markdown": "a"}
SECTIONS = {"data": {"fetched_at_utc": "2026-09-30T09:00:00+00:00"}, "fatigue": {"flagged": 0}}

HYPOTHESIS = {"hypothesis": "x", "suggested_label": "UNRESOLVED", "confidence": "low",
              "supporting_evidence": [], "contradicting_evidence": [], "what_would_confirm": []}


class FakeMessagesClient:
    """A single messages.create call returning one text block."""

    def __init__(self, text, stop_reason="end_turn"):
        self.text, self.stop_reason, self.calls = text, stop_reason, []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(usage=USAGE, stop_reason=self.stop_reason,
                               content=[SimpleNamespace(type="text", text=self.text)])


class ToolTests(unittest.TestCase):
    @needs_extract
    def test_overview_reports_the_known_gaps(self):
        overview = json.loads(llm_agents.dataset_overview())
        self.assertIsInstance(overview["missing_dates"], list)
        self.assertTrue(overview["channels"])

    def test_period_comparison_rejects_unknown_grouping(self):
        self.assertTrue(llm_agents.period_comparison("bogus").startswith("Error"))

    def test_incident_series_refuses_non_aggregate_columns(self):
        result = llm_agents.incident_source_series(INCIDENT, ["LEADID"])
        self.assertTrue(result.startswith("Error"))

    def test_incident_ids_cannot_escape_the_incident_folder(self):
        self.assertTrue(llm_agents.get_incident("../../outputs").startswith("Error"))

    def test_cost_uses_opus_5_5_prices_by_default(self):
        # 10k in at $4/M + 1k out at $20/M
        self.assertAlmostEqual(llm_agents._cost(USAGE), 0.06)


class RunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = llm_agents.ANALYST_ROOT
        llm_agents.ANALYST_ROOT = Path(self.tmp.name)

    def tearDown(self):
        llm_agents.ANALYST_ROOT = self.saved
        self.tmp.cleanup()

    def _brief(self, report: dict, stop_reason: str = "end_turn"):
        import creative_insights
        saved = creative_insights.briefing_sections
        creative_insights.briefing_sections = lambda: SECTIONS
        try:
            client = FakeMessagesClient(json.dumps(report), stop_reason)
            record = llm_agents.run_agent("analyst", client=client)
        finally:
            creative_insights.briefing_sections = saved
        return record, client

    def test_analyst_is_one_call_over_the_briefing_sections(self):
        record, client = self._brief(REPORT)
        self.assertEqual(record["status"], "COMPLETED")
        self.assertEqual(record["output"]["headline"], "h")
        self.assertEqual(record["turns"], 1)
        self.assertAlmostEqual(record["cost_usd"], 0.06)
        self.assertEqual(record["data_as_of"], "2026-09-30T09:00:00+00:00")
        kwargs = client.calls[0]
        self.assertEqual(kwargs["model"], "claude-opus-5-5")
        self.assertEqual(kwargs["output_config"]["format"]["schema"], llm_agents.REPORT_SCHEMA)
        self.assertIn("fatigue", kwargs["messages"][0]["content"])
        self.assertTrue(Path(record["path"]).is_file())

    def test_context_run_validates_and_stores_events(self):
        saved = llm_agents.CONTEXT_ROOT
        llm_agents.CONTEXT_ROOT = Path(self.tmp.name) / "context"
        good = {"title": "t", "start_date": "2026-04-01", "end_date": "", "event_type": "holiday",
                "scope": "national", "expected_effect": "demand_down",
                "source_url": "https://example.org", "source_title": "s", "reason": "r"}
        try:
            client = FakeClient([
                [("replay_cases", {}), ("known_events", {})],
                [("submit_events", {"events": [{**good, "event_type": "rumour"}]})],
                [("submit_events", {"events": [good]})],
            ], USAGE)
            record = llm_agents.run_agent("context", client=client)
        finally:
            llm_agents.CONTEXT_ROOT = saved
        self.assertIn("event_type must be one of", client.runner.results[2])
        self.assertEqual(record["status"], "COMPLETED")
        self.assertEqual(record["output"]["events"][0]["end_date"], "2026-04-01")
        search = [t for t in client.kwargs["tools"] if isinstance(t, dict)][0]
        self.assertEqual(search["max_uses"], 4)
        self.assertEqual(client.kwargs["output_config"], {"effort": "medium"})
        self.assertEqual(client.kwargs["model"], "claude-sonnet-5")
        self.assertEqual(record["model"], "claude-sonnet-5")
        self.assertAlmostEqual(record["cost_usd"], 0.09)  # 3 turns x (10k x $2 + 1k x $10)/M

    def test_monthly_budget_blocks_before_any_call(self):
        saved = llm_agents.spent_this_month
        llm_agents.spent_this_month = lambda: llm_agents.MONTHLY_BUDGET_USD - 0.10
        try:
            with self.assertRaises(RuntimeError) as caught:
                llm_agents.run_agent("analyst", client=FakeClient([], USAGE))
        finally:
            llm_agents.spent_this_month = saved
        self.assertIn("Monthly LLM budget", str(caught.exception))

    def test_web_searches_are_priced(self):
        usage = SimpleNamespace(input_tokens=0, output_tokens=0, cache_read_input_tokens=0,
                                cache_creation_input_tokens=0,
                                server_tool_use=SimpleNamespace(web_search_requests=5))
        self.assertAlmostEqual(llm_agents._cost(usage), 0.05)

    def test_cost_cap_stops_a_run_that_never_submits(self):
        expensive = SimpleNamespace(input_tokens=200_000, output_tokens=20_000,
                                    cache_read_input_tokens=0, cache_creation_input_tokens=0)
        saved = llm_agents.CONTEXT_ROOT
        llm_agents.CONTEXT_ROOT = Path(self.tmp.name) / "context"
        try:
            client = FakeClient([[("known_events", {})]] * 10, expensive)
            record = llm_agents.run_agent("context", client=client)
        finally:
            llm_agents.CONTEXT_ROOT = saved
        self.assertEqual(record["status"], "COST_CAPPED")
        self.assertLess(record["turns"], 10)

    def _investigate(self, output: dict, stop_reason: str = "end_turn"):
        client = FakeMessagesClient(json.dumps(output), stop_reason)
        record = llm_agents.run_agent("investigator", INCIDENT, client=client)
        Path(record["path"]).unlink()  # written into the real incident folder
        return record, client

    @needs_incident
    def test_investigator_is_one_call_with_a_schema(self):
        record, client = self._investigate(HYPOTHESIS)
        self.assertEqual(record["status"], "COMPLETED")
        self.assertEqual(record["turns"], 1)
        self.assertEqual(len(client.calls), 1)
        self.assertAlmostEqual(record["cost_usd"], 0.06)
        kwargs = client.calls[0]
        self.assertEqual(kwargs["max_tokens"], llm_agents.INVESTIGATOR_MAX_TOKENS)
        self.assertEqual(kwargs["output_config"]["effort"], "medium")
        self.assertEqual(kwargs["output_config"]["format"]["schema"], llm_agents.HYPOTHESIS_SCHEMA)
        self.assertNotIn("tools", kwargs)
        self.assertIn(INCIDENT, kwargs["messages"][0]["content"])

    @needs_incident
    def test_investigator_rejects_a_label_outside_the_schema(self):
        record, _ = self._investigate({**HYPOTHESIS, "suggested_label": "CLOSE_IT"})
        self.assertEqual(record["status"], "NO_OUTPUT")
        self.assertIn("allowed values", record["error"])

    @needs_incident
    def test_investigator_refusal_stores_no_output(self):
        record, _ = self._investigate(HYPOTHESIS, stop_reason="refusal")
        self.assertEqual(record["status"], "REFUSED")
        self.assertEqual(record["output"], {})

    @needs_incident
    def test_investigator_evidence_is_aggregate_and_bounded(self):
        evidence = llm_agents.investigator_evidence(INCIDENT)
        columns = set(evidence["daily_series"]["rows"][0]) - {"date", "weekday"}
        self.assertIn("spend_eur_google", columns)
        self.assertTrue(all(c.startswith(("total_", "dashboard_", "spend_eur_", "media_platform_",
                                          "allocator_")) for c in columns))
        self.assertLess(len(json.dumps(evidence)), llm_agents.INVESTIGATOR_MAX_INPUT_CHARS)

    def test_investigator_worst_case_fits_its_cap(self):
        worst = SimpleNamespace(input_tokens=llm_agents.INVESTIGATOR_MAX_INPUT_CHARS // 3,
                                output_tokens=llm_agents.INVESTIGATOR_MAX_TOKENS,
                                cache_read_input_tokens=0, cache_creation_input_tokens=0)
        self.assertLessEqual(llm_agents._cost(worst), llm_agents.MAX_COST_USD["investigator"])

    def test_briefing_sees_the_previous_real_briefings_only(self):
        first, _ = self._brief(REPORT)                                  # a real earlier briefing
        sample = Path(self.tmp.name) / "SAMPLE_x" / "report.json"
        sample.parent.mkdir()
        sample.write_text(json.dumps({"sample": True, "status": "COMPLETED", "output": {"headline": "sample"}}))
        _, client = self._brief(REPORT)
        sent = client.calls[0]["messages"][0]["content"]
        self.assertIn('"previous_briefings": [{"written"', sent)
        self.assertIn("Meta CTR", sent)
        self.assertNotIn('"headline": "sample"', sent)
        self.assertIn("follow_up", llm_agents.REPORT_SCHEMA["required"])

    def test_too_few_findings_store_no_output(self):
        record, _ = self._brief({**REPORT, "findings": REPORT["findings"][:1]})
        self.assertEqual(record["status"], "NO_OUTPUT")
        self.assertIn("3 to 5", record["error"])

    def test_sonnet_is_priced_as_sonnet(self):
        self.assertAlmostEqual(llm_agents._cost(USAGE, "claude-sonnet-5"), 0.03)

    @needs_incident
    def test_investigator_evidence_carries_the_funnel_breakdown(self):
        evidence = llm_agents.investigator_evidence(INCIDENT)
        self.assertIn("funnel_breakdown", evidence)   # the result, or why it is missing

    def test_no_key_means_no_call(self):
        saved = llm_agents.api_key
        llm_agents.api_key = lambda: None
        try:
            with self.assertRaises(RuntimeError):
                llm_agents.run_agent("analyst")
        finally:
            llm_agents.api_key = saved


if __name__ == "__main__":
    unittest.main()
