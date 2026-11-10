"""Offline tests for plan_review.review(): the comments the agents attach to a Planner plan."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import plan_review  # noqa: E402

RUN = {
    "id": 2, "status": "pending", "valid_for": "2026-09-29", "metodo": "classico", "created_at": "2026-09-28T14:31",
    "config": {"data_source": "allocator"},
    "summary": [["Budget speso", 1000.0, ""], ["Lead attesi", 100.0, ""], ["CPL medio", 10.0, ""], ["Combo attive", 5, ""]],
    "channel": [
        {"Channel": "Meta", "Budget": 850.0, "Lead": 90.0, "CPL": 9.5, "Quota_Budget": 0.85},
        {"Channel": "Google", "Budget": 150.0, "Lead": 10.0, "CPL": 10.0, "Quota_Budget": 0.15},
    ],
}
CREATIVE = {
    "channel_overview": {"channels": [
        {"channel": "Meta", "spend_eur": 700.0, "cpl_crm_eur": 9.0, "cpl_crm_change_pct": 5.0},
        {"channel": "Google Search", "spend_eur": 300.0, "cpl_crm_eur": 20.0, "cpl_crm_change_pct": 30.0},
    ]},
    "watchdog": {"status": "WARN", "checks": [
        {"id": "spend_without_crm_leads", "items": [{"channel": "Meta", "ad": "x"}]},
        {"id": "tracking_gap", "items": []}]},
    "fatigue": {"top": []}, "quality_vs_volume": {"channels": {}},
}
LOI = {"calibration": {"planner_outside_ci": ["seller LOI 7"]}, "by_channel": {"channels": []}}


class PlanReviewTests(unittest.TestCase):
    def setUp(self):
        self.review = plan_review.review({"available": True, "run": RUN, "comparison": {
            "b": {"id": 3}, "delta": {"lead_pct": 0.0}, "budget_spostato": 0.0,
            "recalibration": {"stato": "errore", "motivo": "KeyError: segment"}}}, CREATIVE, LOI)
        self.channels = {c["channel"]: c for c in self.review["channels"]}

    def texts(self, channel):
        return " | ".join(c["text"] for c in self.channels[channel]["comments"])

    def test_share_shift_against_last_weeks_spend(self):
        self.assertIn("raises its share of budget to 85% from 70%", self.texts("Meta"))
        self.assertIn("lowers its share of budget to 15% from 30%", self.texts("Google"))

    def test_planner_google_is_matched_to_google_search(self):
        self.assertEqual(self.channels["Google"]["recent_cpl_eur"], 20.0)
        self.assertIn("Planner expects EUR 10.00 per lead; last week's actual was EUR 20.00", self.texts("Google"))
        self.assertIn("rose 30%", self.texts("Google"))

    def test_data_problems_land_on_their_channel(self):
        self.assertIn("spend without reaching the CRM", self.texts("Meta"))
        self.assertNotIn("spend without reaching the CRM", self.texts("Google"))

    def test_plan_level_warnings(self):
        text = " | ".join(c["text"] for c in self.review["plan_comments"])
        self.assertIn("placeholder economics", text)
        self.assertIn("seller LOI 7", text)
        self.assertIn("failed to recalibrate", text)
        self.assertFalse(self.review["economics_real"])

    def test_comments_never_carry_a_budget_instruction(self):
        every = " ".join(c["text"] for ch in self.review["channels"] for c in ch["comments"])
        every += " ".join(c["text"] for c in self.review["plan_comments"])
        for phrase in ("should", "increase the budget", "cut the budget", "move EUR"):
            self.assertNotIn(phrase, every.lower())

    def test_offline_and_empty_states(self):
        self.assertEqual(plan_review.review({"available": False, "error": "down"}, {}, {})["status"], "OFFLINE")
        self.assertEqual(plan_review.review({"available": True, "run": None}, {}, {})["status"], "NO_PLAN")


def _write_export(path, computed="02/10/2026 13:04 UTC"):
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "Riepilogo Run"
    for row in (["Voce", "Valore", "Nota"], ["ID run", 17, ""], ["Stato", "APPROVED", None],
                ["Calcolata il", computed, None], ["Perimetro / giorno_previsto", "2026-10-05", None],
                ["Sorgente dati", "warehouse", None], ["metodo", "classico", None],
                ["Budget speso", 2000.0, ""], ["Lead attesi", 200.0, ""], ["CPL medio", 10.0, None], ["Combo attive", 8, None]):
        ws.append(row)
    pc = wb.create_sheet("Per Canale")
    pc.append(["Channel", "Budget", "Lead", "CPL", "Quota_Budget"])
    pc.append(["Meta", 1600.0, 170.0, 9.41, 0.80])
    pc.append(["Google", 400.0, 30.0, 13.33, 0.20])
    wb.save(path)


class ExportPlanTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.saved = (plan_review.EXPORT_DIR, plan_review._latest_local)
        plan_review.EXPORT_DIR = self.dir
        plan_review._CACHE.clear()

    def tearDown(self):
        plan_review.EXPORT_DIR, plan_review._latest_local = self.saved
        plan_review._CACHE.clear()
        self.tmp.cleanup()

    def test_export_reads_like_a_planner_run(self):
        _write_export(self.dir / "Budget_Allocation_0510.xlsx")
        run = plan_review.plan_from_export(self.dir / "Budget_Allocation_0510.xlsx")
        self.assertEqual((run["id"], run["status"], run["valid_for"], run["created_at"]),
                         (17, "approved", "2026-10-05", "2026-10-02T13:04"))
        review = plan_review.review({"available": True, "run": run}, CREATIVE, LOI)
        self.assertTrue(review["economics_real"])
        self.assertEqual((review["budget_eur"], review["leads"], review["active_cells"]), (2000.0, 200.0, 8))
        self.assertEqual([c["channel"] for c in review["channels"]], ["Meta", "Google"])
        self.assertIn("Production run #17", " | ".join(c["text"] for c in review["plan_comments"]))

    def test_newer_export_wins_older_does_not(self):
        _write_export(self.dir / "Budget_Allocation_0510.xlsx")
        plan_review._latest_local = lambda: {"available": True, "run": dict(RUN), "runs": []}
        self.assertEqual(plan_review.latest_plan()["run"]["id"], 17)            # export 10-02 > local 09-28
        plan_review._CACHE.clear()
        plan_review._latest_local = lambda: {"available": True, "run": dict(RUN, created_at="2026-10-03T09:00"), "runs": []}
        self.assertEqual(plan_review.latest_plan()["run"]["id"], 2)             # local newer

    def test_export_shows_even_when_planner_is_offline(self):
        _write_export(self.dir / "Budget_Allocation_0510.xlsx")
        plan_review._latest_local = lambda: {"available": False, "error": "down"}
        self.assertEqual(plan_review.latest_plan()["run"]["id"], 17)

    def test_unreadable_export_falls_back_to_local(self):
        (self.dir / "Budget_Allocation_9999.xlsx").write_bytes(b"not a workbook")
        plan_review._latest_local = lambda: {"available": True, "run": dict(RUN), "runs": []}
        self.assertEqual(plan_review.latest_plan()["run"]["id"], 2)


if __name__ == "__main__":
    unittest.main()
