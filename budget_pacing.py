"""Budget pacing: do campaigns spend what was configured in Meta and Google Ads? $0.

A delivery check, not Plan vs Actual: it sees the budgets people set in the
platforms (read through the ads-automation project, read-only), not the planner's plan. It
answers two useful questions nothing else here does:
- which campaigns are NOT spending their budget (auction, paused ads, rejected
  creatives -- the money set aside is not going out);
- which campaigns spend ALL of it (budget-limited: Google also says so itself).

Platform rules make daily comparisons noisy (Google may spend up to 2x the
daily budget on a day, Meta balances over the week), so everything is WEEKLY:
last complete week's spend against 7 x the daily budget. The platforms only
return today's budget, so history starts with the first snapshot; until a
snapshot exists for a week, that week is compared with the earliest budget
known and says so.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date
from pathlib import Path

import pandas as pd

import creative_data as cd

APP_ROOT = Path(__file__).resolve().parent
ADS_ROOT = Path(os.getenv("ADS_AUTOMATION_ROOT", str(APP_ROOT.parent / "ads-automation")))
SNAPSHOT_DIR = APP_ROOT / "runtime" / "budget_snapshots"
PACING_PATH = SNAPSHOT_DIR / "pacing.json"

UNDER = 0.60          # spent under 60% of the week's budget: not spending it
FULL = 0.95           # spent 95%+: budget-limited
CHANGED = 1.30        # over 130% of a week's budget no platform rule allows: the budget was different then
MIN_DAILY = 10.0      # campaigns below EUR 10/day are left out


def snapshot(today: date | None = None) -> dict:
    """Read today's budgets (read-only API calls) and store them as budgets_<date>.csv."""
    if str(ADS_ROOT) not in sys.path:
        sys.path.insert(0, str(ADS_ROOT))
    from fetchers.budgets import fetch_budgets

    rows, errors = fetch_budgets()
    today = today or date.today()
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    path = SNAPSHOT_DIR / f"budgets_{today.isoformat()}.csv"
    if rows:
        frame = pd.DataFrame(rows)
        frame.insert(0, "snapshot_date", today.isoformat())
        frame.to_csv(path, index=False)
    return {"path": str(path) if rows else None, "rows": len(rows), "errors": errors}


def load_snapshots() -> pd.DataFrame:
    frames = [pd.read_csv(p, dtype={"campaign_id": str, "adset_id": str, "shared_budget_id": str})
              for p in sorted(SNAPSHOT_DIR.glob("budgets_*.csv"))]
    if not frames:
        return pd.DataFrame()
    frame = pd.concat(frames, ignore_index=True)
    frame["snapshot_date"] = pd.to_datetime(frame["snapshot_date"])
    for column in ("campaign_id", "adset_id", "shared_budget_id"):
        frame[column] = frame[column].fillna("").astype(str).str.replace(r"\.0$", "", regex=True)
    return frame


def campaign_daily_budgets(snapshots: pd.DataFrame) -> pd.DataFrame:
    """One daily budget per campaign per snapshot: the campaign's own (CBO/Google) or the sum of its ad sets (ABO)."""
    if snapshots.empty:
        return snapshots
    # Other business lines are out of every marketing number; their budgets too.
    names_by_id = snapshots.groupby("campaign_id")["campaign"].agg(lambda s: next((v for v in s if isinstance(v, str) and v), ""))
    excluded = {cid for cid, name in names_by_id.items()
                if any(cd.excluded_campaign(ch, name) for ch in snapshots.loc[snapshots["campaign_id"] == cid, "channel"].unique())}
    snapshots = snapshots[~snapshots["campaign_id"].isin(excluded)]
    own = snapshots[(snapshots["level"] == "campaign") & snapshots["daily_budget_eur"].notna()]
    adsets = (snapshots[(snapshots["level"] == "adset") & snapshots["daily_budget_eur"].notna()]
              .groupby(["snapshot_date", "channel", "campaign_id"], as_index=False)["daily_budget_eur"].sum())
    adsets = adsets.merge(own[["snapshot_date", "campaign_id"]], how="left", indicator=True)
    adsets = adsets[adsets["_merge"] == "left_only"].drop(columns="_merge")
    names = snapshots[snapshots["level"] == "campaign"][["campaign_id", "campaign"]].drop_duplicates("campaign_id")
    adsets = adsets.merge(names, on="campaign_id", how="left")
    keep = ["snapshot_date", "channel", "campaign_id", "campaign", "daily_budget_eur", "budget_limited"]
    own = own.reindex(columns=keep)
    adsets = adsets.reindex(columns=keep)
    return pd.concat([own, adsets], ignore_index=True)


def pacing(today: pd.Timestamp | None = None) -> dict:
    """Last complete week: spend vs 7 x daily budget, per campaign and per channel."""
    budgets = campaign_daily_budgets(load_snapshots())
    if budgets.empty:
        return {"status": "NO_SNAPSHOT", "detail": "No budget snapshot yet: Run agents now takes one."}
    spend = cd.spend_rows()
    import creative_insights as ci
    week = ci.last_complete_week(spend)
    week_end = week + pd.Timedelta(days=6)
    in_week = budgets[budgets["snapshot_date"].between(week, week_end)]
    if in_week.empty:
        # History starts with the first snapshot: use the earliest budget known, and say so.
        first = budgets["snapshot_date"].min()
        in_week = budgets[budgets["snapshot_date"] == first]
        basis = f"budgets as of {first.date()} (the first snapshot; earlier budgets are not retrievable)"
        provisional = True
    else:
        basis = f"mean of {in_week['snapshot_date'].nunique()} snapshot(s) in the week"
        provisional = False
    per_campaign = (in_week.groupby(["channel", "campaign_id"], as_index=False)
                    .agg(campaign=("campaign", "first"), daily_budget_eur=("daily_budget_eur", "mean"),
                         budget_limited=("budget_limited", lambda s: any(v is True or str(v).lower() == "true" for v in s))))
    spent = (spend[(spend["week"] == week) & (spend["campaign_id"] != "")]
             .groupby("campaign_id", as_index=False)["spend"].sum())
    per_campaign = per_campaign.merge(spent, on="campaign_id", how="left").fillna({"spend": 0.0})
    per_campaign = per_campaign[per_campaign["daily_budget_eur"] >= MIN_DAILY]
    per_campaign["week_budget_eur"] = per_campaign["daily_budget_eur"] * 7
    per_campaign["utilisation"] = per_campaign["spend"] / per_campaign["week_budget_eur"]

    def verdict(row):
        if row["budget_limited"]:
            return "budget_limited"            # Google's own, current status: reliable
        if row["utilisation"] > CHANGED:
            return "budget_changed"
        if row["utilisation"] >= FULL:
            return "budget_limited"
        if row["utilisation"] < UNDER:
            return "under_delivering"
        return "on_track"
    per_campaign["verdict"] = per_campaign.apply(verdict, axis=1)

    channels = []
    for channel, group in per_campaign.groupby("channel"):
        budget, spent_total = group["week_budget_eur"].sum(), group["spend"].sum()
        channels.append({"channel": channel, "campaigns": int(len(group)),
                         "daily_budget_eur": round(float(group["daily_budget_eur"].sum()), 2),
                         "week_budget_eur": round(float(budget), 2), "week_spend_eur": round(float(spent_total), 2),
                         "utilisation_pct": round(float(100 * spent_total / budget), 1) if budget else None,
                         "under_delivering": int((group["verdict"] == "under_delivering").sum()),
                         "budget_limited": int((group["verdict"] == "budget_limited").sum())})
    campaigns = [{"channel": r["channel"], "campaign": r["campaign"], "daily_budget_eur": round(float(r["daily_budget_eur"]), 2),
                  "week_spend_eur": round(float(r["spend"]), 2), "utilisation_pct": round(float(100 * r["utilisation"]), 1),
                  "verdict": r["verdict"], "google_says_limited": bool(r["budget_limited"])}
                 for _, r in per_campaign.sort_values("week_budget_eur", ascending=False).iterrows()]
    flagged = [c for c in campaigns if c["verdict"] != "on_track"]
    return {"status": "WARN" if flagged else "HEALTHY", "week": str(week.date()), "basis": basis, "provisional": provisional,
            "snapshots": int(load_snapshots()["snapshot_date"].nunique()),
            "latest_snapshot": str(budgets["snapshot_date"].max().date()),
            "channels": sorted(channels, key=lambda c: -c["week_budget_eur"]), "campaigns": campaigns,
            "flagged": flagged,
            "rule": (f"weekly spend vs 7 x daily budget; under {int(UNDER * 100)}% = not spending its budget, "
                     f"{int(FULL * 100)}%+ or Google's own 'limited by budget' = budget-limited, over {int(CHANGED * 100)}% = "
                     f"the budget was different that week; campaigns under EUR {MIN_DAILY:.0f}/day left out"),
            "note": ("Budgets as set in Meta and Google Ads, not the planner's plan. Outbrain and TikTok are not covered yet."
                     + (" PROVISIONAL: no snapshot falls inside this week yet, so today's budgets stand in for last week's;"
                        " only Google's own 'limited by budget' is certain. Verdicts firm up from next week." if provisional else ""))}


def refresh() -> dict:
    """Snapshot today's budgets and recompute pacing. Aggregates and campaign names only."""
    taken = snapshot()
    result = pacing()
    result["snapshot"] = taken
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    PACING_PATH.write_text(json.dumps(result, default=str), encoding="utf-8")
    return result


def cached() -> dict | None:
    return json.loads(PACING_PATH.read_text(encoding="utf-8")) if PACING_PATH.is_file() else None
