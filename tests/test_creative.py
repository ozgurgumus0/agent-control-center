"""Offline tests for creative_data and creative_insights on a small synthetic sheet.

No test here reaches Google Sheets or the Anthropic API.
"""
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import creative_data as cd  # noqa: E402
import creative_insights as ci  # noqa: E402

DAYS = pd.date_range("2026-08-03", "2026-09-27")          # 8 full Mon-Sun weeks
LAST_WEEK = pd.Timestamp("2026-09-21")


def build_sheet(folder: Path) -> None:
    spend, funnel = [], []
    for day in DAYS:
        late = day >= LAST_WEEK
        d = day.strftime("%Y-%m-%d")
        # Meta ad A: CTR halves in the last week (fatigue-like), leads tied by exact UTM.
        spend.append([d, "Nord_CBO", "Ad_A", "1", 100, 10000, 50 if late else 100, 10, "Meta"])
        funnel.append(["Meta", "Nord_CBO", "Ad_A", d, 8, 1, 6, 3, 2, 0, 0, 1, 0, 0.1])
        # Meta ad B: spends, has platform leads, but no CRM row carries its name.
        spend.append([d, "Nord_CBO", "Ad_B", "2", 50, 4000, 40, 4, "Meta"])
        # TikTok: UTM Content = variant prefix + the ad name.
        spend.append([d, "Smart", "PROMO_X", "3", 20, 3000, 30, 3, "TikTok"])
        funnel.append(["tiktok", "Smart", "20260101_000000-1_PROMO_X", d, 2, 0, 2, 1, 1, 0, 0, 0, 0, 0])
        # Google Search: CPM rises in the last week; New_Data spells the campaign with '::PV'.
        spend.append([d, "Nord_High", "", "", 200 if late else 100, 1000, 100, 10, "Google Search"])
        funnel.append(["Google", "Nord_High::PV", "", d, 9, 1, 7, 4, 3, 0, 0, 0, 0, 0.2])
        # Excluded business line and a recruiting campaign.
        spend.append([d, "PMAX_OtherBU::BU", "Asset Group 1", "4", 999, 1, 1, 1, "PMax"])
        spend.append([d, "HR_Campaign_2026", "Hiring", "5", 7, 100, 1, 0, "Meta"])
    spend_cols = ["date", "campaign", "ad", "ad_id", "spend", "impressions", "clicks", "platform_leads", "channel"]
    funnel_cols = ["channel", "campaign", "utm", "date", "crm_leads", "low_prio", "called", "sc1_booked",
                   "sc1_net", "loi1", "loi4", "loi6", "loi7", "contracts"]
    pd.DataFrame(spend, columns=spend_cols).to_csv(folder / "spend.csv", index=False)
    pd.DataFrame(funnel, columns=funnel_cols).to_csv(folder / "funnel.csv", index=False)
    details = pd.DataFrame([
        ["Ad_A", "PV", "Meta", "video", "Bonus", "state-incentives", "brand", "indoor", "slow", "9:16", "yes", "m"],
        ["PROMO_X", "HP", "TikTok", "video", "", "price-objection", "customer", "outdoor", "fast", "9:16", "no", "m"],
    ], columns=list(cd.DETAIL_COLUMNS.values()))
    details.to_csv(folder / "details.csv", index=False)
    (folder / "meta.json").write_text(json.dumps({"fetched_at_utc": "2026-09-28T08:00:00+00:00", "tabs": {}}))


class CreativeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.saved = cd.CACHE_DIR
        cd.CACHE_DIR = Path(cls.tmp.name)
        cd._MEMO.clear()
        build_sheet(cd.CACHE_DIR)

    @classmethod
    def tearDownClass(cls):
        cd.CACHE_DIR = cls.saved
        cd._MEMO.clear()
        cls.tmp.cleanup()

    def test_channels_are_normalised_like_the_dashboard(self):
        self.assertEqual(cd.norm_channel("Google"), "Google Search")
        self.assertEqual(cd.norm_channel("tiktok"), "TikTok")
        self.assertEqual(set(cd.funnel_rows()["channel"]), {"Meta", "TikTok", "Google Search"})

    def test_excluded_and_non_marketing_campaigns_stay_out(self):
        rows = cd.spend_rows()
        self.assertNotIn("PMax", set(rows["channel"]))
        self.assertFalse(rows["campaign"].str.startswith("HR_").any())
        self.assertTrue(cd.spend_rows(include_non_marketing=True)["non_marketing"].any())
        self.assertFalse(cd.NON_MARKETING.search("Nord_Three_CBO"))

    def test_funnel_rows_tie_to_ads_exactly_or_by_tiktok_suffix(self):
        matched = cd.match_funnel_to_ads(cd.spend_rows(), cd.funnel_rows())
        keys = dict(zip(matched["channel"], matched["ad_key"]))
        self.assertEqual(keys["Meta"], "ad_a")
        self.assertEqual(keys["TikTok"], "promo_x")
        self.assertEqual(keys["Google Search"], "")      # blank UTM: channel level only

    def test_campaign_loose_key_matches_a_unique_owner(self):
        weekly = cd.campaign_weekly("Google Search")
        self.assertGreater(weekly["crm_leads"].sum(), 0)

    def test_funnel_breakdown_splits_the_cpl_change_exactly(self):
        result = ci.funnel_breakdown("Google Search", LAST_WEEK)
        shares = [v["share_of_change_pct"] for v in result["cpl_split"].values()]
        self.assertAlmostEqual(sum(shares), 100.0, places=0)
        logs = sum(v["log_points"] for v in result["cpl_split"].values())
        ratio = result["now"]["cpl_crm_eur"] / result["baseline_values"]["cpl_crm_eur"]
        self.assertAlmostEqual(math.exp(logs), ratio, places=2)
        # Spend doubled on the same impressions: the CPM step carries all of it.
        self.assertAlmostEqual(result["cpl_split"]["cpm"]["share_of_change_pct"], 100.0, places=0)
        self.assertFalse(result["sc1_mature"])          # funnel ends 6 days after that week

    def test_fatigue_needs_the_impressions_floor(self):
        self.assertEqual(ci.fatigue()["flagged"], 1)
        self.assertEqual(ci.fatigue()["top"][0]["ad"], "Ad_A")

    def test_product_mix_only_counts_channels_that_tie_leads(self):
        mix = ci.product_mix()
        self.assertEqual(mix["channels"], ["Meta", "TikTok"])
        products = {row["value"] for row in mix["by"]["product"]}
        self.assertEqual(products, {"PV", "HP"})

    def test_checks_flag_the_creative_that_never_reaches_the_crm(self):
        checks = {c["id"]: c for c in ci.creative_checks(today=pd.Timestamp("2026-09-28"))["checks"]}
        orphans = [item["ad"] for item in checks["spend_without_crm_leads"]["items"]]
        self.assertEqual(orphans, ["Ad_B"])
        self.assertEqual(checks["non_marketing_spend"]["status"], "warn")
        stale = {c["id"]: c for c in ci.creative_checks(today=pd.Timestamp("2026-10-20"))["checks"]}
        self.assertEqual(stale["freshness"]["status"], "warn")

    def test_briefing_is_small_enough_for_one_call(self):
        import llm_agents
        size = len(json.dumps(ci.briefing_sections(), default=str))
        self.assertLess(size, llm_agents.SINGLE_CALL_MAX_INPUT_CHARS)


if __name__ == "__main__":
    unittest.main()
