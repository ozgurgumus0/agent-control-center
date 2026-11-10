"""Offline tests for loi_feed on a synthetic workbook with planted identifiers."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import loi_feed  # noqa: E402

SECRET_LEAD = "LEAD-SECRET-4242"
SECRET_URL = "https://meeting.example.com/j/000000000?pwd=SECRET"

# A synthetic ev_score.sql with made-up probabilities, in the shape
# loi_feed.planner_mapping() parses. The real file lives in the planner project.
EV_SQL = """
loi_per_lead AS (
  SELECT CASE
    WHEN fa.neg = 1 THEN 0.0
    WHEN fa.cr < 40 THEN 0.02
    WHEN fa.cr < 60 THEN 0.03
    WHEN fa.cr < 70 THEN 0.05
    WHEN fa.cr < 80 THEN 0.08
    ELSE 0.15 END AS p_sign FROM fa
  UNION ALL
  SELECT CASE s.loi WHEN 1 THEN 0.02 WHEN 4 THEN 0.04 WHEN 6 THEN 0.06 WHEN 7 THEN 0.12 END FROM s
)
"""


def use_fake_ev_sql(folder: Path):
    """Point loi_feed at the synthetic SQL; returns the previous path to restore."""
    saved = loi_feed.PLANNER_EV_SQL
    path = folder / "ev_score.sql"
    path.write_text(EV_SQL, encoding="utf-8")
    loi_feed.PLANNER_EV_SQL = path
    return saved


def build_workbook(path: Path) -> None:
    rng = np.random.default_rng(7)
    rows = []
    starts = pd.date_range("2026-06-01", "2026-09-27", freq="3h")
    for i, start in enumerate(starts):
        status = "scored" if i % 5 else "cancelled"
        readiness = float(rng.choice([30, 55, 65, 75, 85])) if status == "scored" else np.nan
        band = {30: "Freddo", 55: "Tiepido", 65: "Calduccio", 75: "Calduccio", 85: "Caldo"}.get(readiness)
        if status == "scored" and i % 17 == 0:
            band, readiness = "Non idoneo", np.nan
        # Signing climbs with readiness so the order check has something to hold.
        p = {30: .02, 55: .03, 65: .06, 75: .09, 85: .2}.get(readiness, .01)
        rows.append({"id": f"row{i}", "leadid": SECRET_LEAD, "hostId": "host-secret", "joinUrl": SECRET_URL,
                     "startTime": start.isoformat(), "loi_match_status": status, "loi_band": band if status == "scored" else None,
                     "closing_readiness": readiness, "channel": "facebook" if start < pd.Timestamp("2026-07-01") else None,
                     "region": "Lazio" if start < pd.Timestamp("2026-07-01") else None, "Seller LOI": 4.0,
                     "Firmato_Post_SC1_Gross": "Si" if rng.random() < p else "No",
                     "Firmato_Post_SC1_Net": "Si" if rng.random() < p else "No"})
    pd.DataFrame(rows).to_excel(path, index=False)


class LoiFeedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.saved_sql = use_fake_ev_sql(Path(cls.tmp.name))
        folder = Path(cls.tmp.name) / "loi_totali_99_2026-09-28"
        folder.mkdir()
        cls.path = folder / "LOI Totali 99_filled.xlsx"
        build_workbook(cls.path)
        cls.saved_reasons = loi_feed.REASON_DIR
        loi_feed.REASON_DIR = Path(cls.tmp.name) / "reason_aggregates"
        loi_feed.REASON_DIR.mkdir()
        (loi_feed.REASON_DIR / "reason_aggregates_2026-09-15.json").write_text(json.dumps({
            "generated_at_utc": "2026-09-15T08:00:00+00:00", "through_week": "2026-09-08",
            "weeks": [], "definitions": {}, "note": "n"}))
        loi_feed._MEMO.clear()
        cls.feed = loi_feed.feed(cls.path, today=pd.Timestamp("2026-09-28"))

    @classmethod
    def tearDownClass(cls):
        loi_feed.REASON_DIR = cls.saved_reasons
        loi_feed.PLANNER_EV_SQL = cls.saved_sql
        loi_feed._MEMO.clear()
        cls.tmp.cleanup()

    def test_identifier_columns_are_never_read(self):
        columns = set(loi_feed.load(self.path).columns)
        self.assertFalse(columns & {"leadid", "hostId", "joinUrl", "id", "meetingId", "meetingUuid"})

    def test_no_identifier_reaches_the_output(self):
        text = json.dumps(self.feed, default=str)
        self.assertNotIn(SECRET_LEAD, text)
        self.assertNotIn("SECRET", text)
        self.assertNotIn("host-secret", text)

    def test_planner_rates_are_read_from_its_own_sql(self):
        mapping = loi_feed.planner_mapping()
        self.assertEqual([b["planner_p"] for b in mapping["ai_bands"]], [0.02, 0.03, 0.05, 0.08, 0.15])
        self.assertEqual(mapping["negative_p"], 0.0)
        self.assertEqual(mapping["seller"], {1: 0.02, 4: 0.04, 6: 0.06, 7: 0.12})

    def test_calibration_uses_only_mature_calls_and_suppresses_small_groups(self):
        cal = self.feed["calibration"]
        self.assertEqual(cal["mature_until"], "2026-08-18")        # latest start 09-27 minus 40 days
        for row in cal["ai_bands"] + cal["seller_fallback"]:
            if row["calls"] < loi_feed.MIN_GROUP:
                self.assertIsNone(row["rate_pct"])

    def test_channel_view_is_marked_historical(self):
        by_channel = self.feed["by_channel"]
        self.assertEqual(by_channel["channel_tags_end"][:7], "2026-06")
        self.assertEqual(by_channel["channels"][0]["channel"], "Meta")
        checks = {c["id"]: c for c in self.feed["checks"]}
        self.assertEqual(checks["loi_channel_tags"]["status"], "warn")
        self.assertEqual(checks["loi_freshness"]["status"], "ok")

    def test_weekly_quality_ends_on_the_last_complete_week(self):
        weekly = self.feed["weekly_quality"]
        self.assertEqual(weekly["through_week"], "2026-09-21")
        self.assertEqual(len(weekly["weeks"]), 8)

    def test_reason_feed_is_carried_and_its_age_checked(self):
        self.assertEqual(self.feed["reasons"]["through_week"], "2026-09-08")
        checks = {c["id"]: c for c in self.feed["checks"]}
        self.assertEqual(checks["loi_reasons_fresh"]["status"], "warn")   # 13 days old > 8


class SheetSnapshotTests(unittest.TestCase):
    """The daily scoring run's sheet snapshot wins when it is newer than the Totali workbook."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.saved_sql = use_fake_ev_sql(root)
        folder = root / "loi_totali_99_2026-09-28"
        folder.mkdir()
        build_workbook(folder / "LOI Totali 99_filled.xlsx")
        frame = pd.read_excel(folder / "LOI Totali 99_filled.xlsx").drop(columns=["Seller LOI"])
        later = pd.DataFrame([{"id": "late", "leadid": SECRET_LEAD, "joinUrl": SECRET_URL,
                               "startTime": "2026-10-09T07:00:00.000Z", "loi_match_status": "scored",
                               "loi_band": "Caldo", "closing_readiness": "85"}])
        frame = pd.concat([frame, later], ignore_index=True).astype(object).where(lambda f: f.notna(), "")
        snap = root / "gsheet_score_2026-10-12"
        snap.mkdir()
        cls.snapshot = snap / "sheet_before.json"
        cls.snapshot.write_text(json.dumps([list(frame.columns)] + frame.astype(str).values.tolist()))
        cls.saved = loi_feed.LOI_OUTPUTS
        loi_feed.LOI_OUTPUTS = root
        loi_feed._MEMO.clear()
        cls.feed = loi_feed.feed(today=pd.Timestamp("2026-10-12"))

    @classmethod
    def tearDownClass(cls):
        loi_feed.LOI_OUTPUTS = cls.saved
        loi_feed.PLANNER_EV_SQL = cls.saved_sql
        loi_feed._MEMO.clear()
        cls.tmp.cleanup()

    def test_newer_snapshot_is_the_source(self):
        self.assertEqual(loi_feed.newest_file(), self.snapshot)
        self.assertEqual(self.feed["latest_scored_call"], "2026-10-09")
        self.assertIn("daily sheet snapshot", self.feed["source"])

    def test_seller_fallback_still_comes_from_the_workbook(self):
        seller4 = [r for r in self.feed["calibration"]["seller_fallback"] if r["band"] == "seller LOI 4"][0]
        self.assertGreater(seller4["calls"], 0)

    def test_no_identifier_reaches_the_output(self):
        text = json.dumps(self.feed, default=str)
        self.assertNotIn("SECRET", text)
        self.assertFalse(set(loi_feed.load(self.snapshot).columns) & {"leadid", "joinUrl", "id"})


if __name__ == "__main__":
    unittest.main()
