"""The briefing PDF builds from any state, including an empty one, and never shows a layout sample."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import briefing_pdf  # noqa: E402

STATE = {
    "llm": {"analyst_report": {"sample": True, "status": "COMPLETED", "model": "SAMPLE",
                               "output": {"headline": "SAMPLE HEADLINE", "findings": []}}},
    "allocation": {"status": "OFFLINE", "detail": "Planner backend not reachable"},
    "incidents": [], "freshness": [{"label": "CRM export", "date": "2026-06-22", "age_days": 100, "stale": True}],
    "creative": {"watchdog": {"checks": [{"id": "non_marketing_spend", "title": "Only marketing spend in marketing metrics",
                                          "status": "warn", "detail": "EUR 48.45 in 28 days"}]}},
    "loi": {},
}


class BriefingPdfTests(unittest.TestCase):
    def test_builds_from_a_thin_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = briefing_pdf.build(STATE, Path(tmp))
            data = path.read_bytes()
        self.assertTrue(data.startswith(b"%PDF"))
        self.assertGreater(len(data), 2000)

    def test_sample_briefing_is_never_shown(self):
        flow = briefing_pdf.briefing_section(STATE)
        text = " ".join(getattr(f, "text", "") for f in flow)
        self.assertNotIn("SAMPLE HEADLINE", text)
        self.assertIn("has not been generated", " ".join(str(f) for f in flow))

    def test_warnings_name_the_problem(self):
        items = dict(briefing_pdf.attention(STATE))
        self.assertIn("Non-marketing spend inside main_metrics", items)
        self.assertIn("CRM export is 100 days old", items)

    def test_only_real_figures_are_bold(self):
        out = briefing_pdf.bold_numbers("Plan for 29 Sep 2026: EUR 2,330, 234 leads, 85% share, LOI 4.")
        self.assertIn("<b>2,330</b>", out)
        self.assertIn("<b>85%</b>", out)
        self.assertNotIn("<b>2026</b>", out)
        self.assertNotIn("<b>29</b>", out)
        self.assertNotIn("<b>4</b>", out)


if __name__ == "__main__":
    unittest.main()
