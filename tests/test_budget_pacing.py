"""Offline tests for budget_pacing: CBO vs ABO, exclusions, verdicts, provisional weeks."""
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import budget_pacing as bp  # noqa: E402
import creative_data as cd  # noqa: E402

WEEK = pd.Timestamp("2026-09-21")


def snapshot_rows(day: str) -> list[dict]:
    base = {"snapshot_date": day, "lifetime_budget_eur": None, "shared_budget_id": "", "status": "ACTIVE"}
    return [
        # CBO: the budget sits on the campaign.
        {**base, "channel": "Meta", "level": "campaign", "campaign_id": "1", "campaign": "Lazio_CBO", "adset_id": "", "adset": "",
         "daily_budget_eur": 100.0, "budget_limited": None},
        # ABO: no campaign budget, two ad sets carry it.
        {**base, "channel": "Meta", "level": "campaign", "campaign_id": "2", "campaign": "Nord_ABO", "adset_id": "", "adset": "",
         "daily_budget_eur": None, "budget_limited": None},
        {**base, "channel": "Meta", "level": "adset", "campaign_id": "2", "campaign": "", "adset_id": "21", "adset": "a",
         "daily_budget_eur": 30.0, "budget_limited": None},
        {**base, "channel": "Meta", "level": "adset", "campaign_id": "2", "campaign": "", "adset_id": "22", "adset": "b",
         "daily_budget_eur": 20.0, "budget_limited": None},
        # Google says limited, and spends all of it.
        {**base, "channel": "Google Search", "level": "campaign", "campaign_id": "3", "campaign": "Nord_High", "adset_id": "",
         "adset": "", "daily_budget_eur": 50.0, "budget_limited": True},
        # Other business line: excluded like its spend.
        {**base, "channel": "Google Search", "level": "campaign", "campaign_id": "4", "campaign": "OtherBU", "adset_id": "",
         "adset": "", "daily_budget_eur": 80.0, "budget_limited": True},
        # Spent far over 7x its budget: the budget was different that week.
        {**base, "channel": "Meta", "level": "campaign", "campaign_id": "5", "campaign": "Test_X", "adset_id": "", "adset": "",
         "daily_budget_eur": 10.0, "budget_limited": None},
    ]


SPEND = pd.DataFrame([
    {"campaign_id": "1", "spend": 200.0}, {"campaign_id": "2", "spend": 280.0},
    {"campaign_id": "3", "spend": 350.0}, {"campaign_id": "5", "spend": 200.0},
]).assign(week=WEEK, date=WEEK + pd.Timedelta(days=6), channel="x")


class PacingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = (bp.SNAPSHOT_DIR, cd.spend_rows)
        bp.SNAPSHOT_DIR = Path(self.tmp.name)
        cd.spend_rows = lambda include_non_marketing=False: SPEND.copy()

    def tearDown(self):
        bp.SNAPSHOT_DIR, cd.spend_rows = self.saved
        self.tmp.cleanup()

    def write(self, day):
        pd.DataFrame(snapshot_rows(day)).to_csv(bp.SNAPSHOT_DIR / f"budgets_{day}.csv", index=False)

    def verdicts(self, result):
        return {c["campaign"]: c["verdict"] for c in result["campaigns"]}

    def test_week_with_a_snapshot_is_firm(self):
        self.write("2026-09-24")
        result = bp.pacing()
        self.assertFalse(result["provisional"])
        v = self.verdicts(result)
        self.assertEqual(v["Lazio_CBO"], "under_delivering")        # 200 of 700
        self.assertEqual(v["Nord_ABO"], "on_track")                 # 280 of 7 x (30 + 20) = 350, i.e. 80%
        self.assertEqual(v["Nord_High"], "budget_limited")          # Google's own status
        self.assertEqual(v["Test_X"], "budget_changed")             # 200 of 70
        self.assertNotIn("OtherBU", v)

    def test_first_snapshot_after_the_week_is_provisional(self):
        self.write("2026-09-30")
        result = bp.pacing()
        self.assertTrue(result["provisional"])
        self.assertIn("PROVISIONAL", result["note"])

    def test_channel_totals(self):
        self.write("2026-09-24")
        meta = next(c for c in bp.pacing()["channels"] if c["channel"] == "Meta")
        self.assertEqual(meta["daily_budget_eur"], 160.0)           # 100 + 50 + 10
        self.assertEqual(meta["week_spend_eur"], 680.0)

    def test_no_snapshot_says_so(self):
        self.assertEqual(bp.pacing()["status"], "NO_SNAPSHOT")


if __name__ == "__main__":
    unittest.main()
