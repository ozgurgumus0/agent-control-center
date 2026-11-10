"""The questions the agents answer from creative ad performance. Deterministic, $0.

Three consumers:
- creative_checks()   -> Data Watchdog: is the creative data complete and consistent?
- funnel_breakdown()  -> Investigator: which step of the funnel moved, and where?
- briefing_sections() -> weekly briefing: fatigue, quality vs volume, creative
  attributes, product mix. The LLM only turns these into prose.

Everything returns plain dicts of aggregates. No function here says what to do
with a budget; they describe what the data shows.

Maturity matters: a lead created last week has not had time to reach an SC1,
and contracts take weeks more. Quality metrics therefore use weeks at least
SC1_LAG_DAYS old, and contract counts younger than CONTRACT_LAG_WEEKS are
marked immature instead of being read as low.
"""
from __future__ import annotations

import math

import pandas as pd

import creative_data as cd

SC1_LAG_DAYS = 14
CONTRACT_LAG_WEEKS = 8
BASELINE_WEEKS = 4
PLATFORM_LEAD_CHANNELS = ("Meta", "Google Search", "TikTok", "Outbrain", "PMax")
CREATIVE_CHANNELS = ("Meta", "TikTok")          # where CRM leads can be tied to an ad
ATTRIBUTES = ("hook_category", "pov", "setting", "edit_pace", "format", "person_on_screen")


# ------------------------------------------------------------------- helpers

def _ratio(a, b):
    return float(a) / float(b) if b else None


def _pct_change(new, old):
    return round((new / old - 1) * 100, 1) if new is not None and old else None


def _r(value, digits=2):
    return None if value is None or (isinstance(value, float) and math.isnan(value)) else round(value, digits)


def last_complete_week(spend: pd.DataFrame) -> pd.Timestamp:
    """The latest Monday whose week (Mon-Sun) is fully covered by spend data."""
    max_date = spend["date"].max().normalize()
    this_monday = max_date - pd.Timedelta(days=max_date.weekday())
    return this_monday if max_date.weekday() == 6 else this_monday - pd.Timedelta(days=7)


def _funnel_max_date() -> pd.Timestamp:
    return cd.funnel_rows()["date"].max().normalize()


def _mature_until() -> pd.Timestamp:
    """Weeks starting on or before this date have had SC1_LAG_DAYS to book SC1s."""
    return _funnel_max_date() - pd.Timedelta(days=SC1_LAG_DAYS + 6)


def _stages(row) -> dict:
    spend, impr, clicks = row["spend"], row["impressions"], row["clicks"]
    platform, crm, sc1 = row["platform_leads"], row["crm_leads"], row["sc1_net"]
    return {
        "spend_eur": _r(spend), "crm_leads": _r(crm, 1), "sc1_net": _r(sc1, 1),
        "cpm_eur": _r(_ratio(spend * 1000, impr)), "ctr_pct": _r(100 * _ratio(clicks, impr), 3) if impr else None,
        "click_to_platform_lead_pct": _r(100 * _ratio(platform, clicks), 2) if clicks else None,
        "platform_to_crm_lead_ratio": _r(_ratio(crm, platform), 3),
        "cpl_crm_eur": _r(_ratio(spend, crm)), "sc1_rate_pct": _r(100 * _ratio(sc1, crm), 1) if crm else None,
        "cost_per_sc1_eur": _r(_ratio(spend, sc1)),
        "low_prio_share_pct": _r(100 * _ratio(row["low_prio"], crm), 1) if crm else None,
        "rep_caldo_share_pct": _r(100 * _ratio(row["loi7"], crm), 2) if crm else None,
        "contracts": _r(row["contracts"], 1),
    }


# ------------------------------------------------------------ watchdog checks

def creative_checks(today: pd.Timestamp | None = None) -> dict:
    """Integrity checks on the creative data. Each check: ok or warn, with its evidence."""
    meta = cd.meta()
    if meta is None:
        return {"status": "NO_DATA", "checks": [], "detail": "Creative data never fetched."}
    today = (today or pd.Timestamp.now()).normalize()
    spend = cd.spend_rows(include_non_marketing=True)
    marketing = spend[~spend["non_marketing"]]
    checks = []

    spend_age = (today - spend["date"].max().normalize()).days
    funnel_age = (today - _funnel_max_date()).days
    cache_age = (pd.Timestamp.now(tz="UTC") - pd.Timestamp(meta["fetched_at_utc"])).total_seconds() / 3600
    checks.append({
        "id": "freshness", "title": "Creative data is current",
        "status": "warn" if spend_age > 2 or funnel_age > 4 else "ok",
        "detail": f"Spend to {spend['date'].max().date()} ({spend_age}d old), funnel to "
                  f"{_funnel_max_date().date()} ({funnel_age}d old); fetched {cache_age:.0f}h ago.",
        "items": []})

    weekly = cd.channel_weekly()
    week = last_complete_week(marketing)
    weekly = weekly[(weekly["week"] <= week) & (weekly["week"] >= marketing["week"].min())]
    gaps = []
    for channel in PLATFORM_LEAD_CHANNELS:
        rows = weekly[weekly["channel"] == channel].set_index("week").sort_index()
        if week not in rows.index or rows.loc[week, "platform_leads"] < 30:
            continue
        ratio = rows["crm_leads"] / rows["platform_leads"].where(rows["platform_leads"] > 0)
        base = ratio.loc[:week - pd.Timedelta(days=7)].tail(8).median()
        now = ratio.loc[week]
        if base and now and abs(now / base - 1) >= 0.35:
            gaps.append({"channel": channel, "week": str(week.date()),
                         "crm_per_platform_lead": _r(now, 3), "usual": _r(base, 3),
                         "change_pct": _pct_change(now, base)})
    checks.append({
        "id": "tracking_gap", "title": "Platform leads and CRM leads move together",
        "status": "warn" if gaps else "ok",
        "detail": (f"{len(gaps)} channel(s) where CRM leads per platform lead shifted >=35% in the "
                   f"week of {week.date()} vs the prior 8 weeks: a tracking or sync break is one "
                   f"explanation." if gaps else f"Week of {week.date()}: ratios within 35% of the usual level."),
        "items": gaps})

    recent = marketing[marketing["date"] > today - pd.Timedelta(days=14)]
    matched = cd.match_funnel_to_ads(marketing, cd.funnel_rows())
    leads_by_ad = (matched[(matched["ad_key"] != "") & (matched["date"] > today - pd.Timedelta(days=14))]
                   .groupby(["channel", "ad_key"])["crm_leads"].sum())
    orphans = []
    for (channel, ad_key), group in recent[recent["channel"].isin(CREATIVE_CHANNELS)].groupby(["channel", "ad_key"]):
        if not ad_key or group["spend"].sum() < 300:
            continue
        if leads_by_ad.get((channel, ad_key), 0) == 0 and group["platform_leads"].sum() > 0:
            orphans.append({"channel": channel, "ad": group["ad"].iloc[0],
                            "spend_eur_14d": _r(group["spend"].sum()),
                            "platform_leads_14d": _r(group["platform_leads"].sum(), 0)})
    orphans.sort(key=lambda item: -item["spend_eur_14d"])
    checks.append({
        "id": "spend_without_crm_leads", "title": "Every spending creative reaches the CRM",
        "status": "warn" if orphans else "ok",
        "detail": (f"{len(orphans)} Meta/TikTok creative(s) spent >=EUR 300 in 14 days with platform "
                   f"leads but no CRM lead carrying their name: a missing or wrong utm_content is the "
                   f"usual cause." if orphans else "All Meta/TikTok creatives with spend reach the CRM."),
        "items": orphans[:10]})

    hr = spend[spend["non_marketing"] & (spend["date"] > today - pd.Timedelta(days=28))]
    items = [{"campaign": name, "spend_eur_28d": _r(group["spend"].sum())}
             for name, group in hr.groupby("campaign")]
    checks.append({
        "id": "non_marketing_spend", "title": "Only marketing spend in marketing metrics",
        "status": "warn" if items else "ok",
        "detail": (f"EUR {sum(i['spend_eur_28d'] for i in items):,.2f} in 28 days on non-marketing "
                   f"campaigns; excluded from every metric here, but still inside main_metrics totals."
                   if items else "No recruiting or other non-marketing campaigns in the last 28 days."),
        "items": items})

    detail = cd.details()
    tagged = set(detail.loc[detail["hook_category"].str.strip() != "", "ad_key"])
    last28 = marketing[(marketing["date"] > today - pd.Timedelta(days=28))
                       & marketing["channel"].isin(["Meta", "TikTok", "Outbrain", "Youtube", "Display"])]
    by_ad = last28[last28["ad_key"] != ""].groupby(["channel", "ad_key"]).agg(
        ad=("ad", "first"), spend=("spend", "sum"))
    untagged = by_ad[(by_ad["spend"] >= 200) & ~by_ad.index.get_level_values("ad_key").isin(tagged)]
    share = _ratio(untagged["spend"].sum(), by_ad["spend"].sum())
    checks.append({
        "id": "untagged_creatives", "title": "Spending creatives are described in Ad Details",
        "status": "warn" if share and share > 0.2 else "ok",
        "detail": f"{len(untagged)} creative(s) with >=EUR 200 in 28 days have no Ad Details "
                  f"attributes ({100 * (share or 0):.0f}% of that spend): they are invisible to the "
                  f"attribute analysis.",
        "items": [{"channel": ch, "ad": row["ad"], "spend_eur_28d": _r(row["spend"])}
                  for (ch, _), row in untagged.sort_values("spend", ascending=False).head(10).iterrows()]})

    checks.append({
        "id": "creative_coverage", "title": "Share of CRM leads that can be tied to an ad (28 days)",
        "status": "ok", "detail": "Informational: where the per-creative analysis is possible.",
        "items": [{"channel": ch, **v} for ch, v in cd.funnel_coverage(today - pd.Timedelta(days=28)).items()]})

    status = "WARN" if any(c["status"] == "warn" for c in checks) else "HEALTHY"
    return {"status": status, "as_of": str(today.date()), "checks": checks}


# --------------------------------------------------------- funnel breakdown

def funnel_breakdown(channel: str, week: pd.Timestamp | None = None,
                     baseline_weeks: int = BASELINE_WEEKS) -> dict:
    """Split the change in a channel's cost per CRM lead into the funnel steps behind it.

    cost per CRM lead = CPM/1000 / CTR / (platform leads per click) / (CRM leads per platform lead),
    so the log change splits exactly into four step contributions.
    """
    weekly = cd.channel_weekly()
    rows = weekly[weekly["channel"] == channel].set_index("week").sort_index()
    spend = cd.spend_rows()
    week = pd.Timestamp(week) if week is not None else last_complete_week(spend)
    week = week - pd.Timedelta(days=week.weekday())
    if week not in rows.index:
        return {"channel": channel, "week": str(week.date()), "error": "No data for that channel and week."}
    base_index = [w for w in rows.index if w < week and w >= spend["week"].min()][-baseline_weeks:]
    if not base_index:
        return {"channel": channel, "week": str(week.date()), "error": "No earlier weeks to compare with."}
    now, base = rows.loc[week], rows.loc[base_index].sum()
    stages_now, stages_base = _stages(now), _stages(base)
    stages_base["spend_eur"] = _r(base["spend"] / len(base_index))
    stages_base["crm_leads"] = _r(base["crm_leads"] / len(base_index), 1)

    factors = {"cpm": (now["spend"] / now["impressions"], base["spend"] / base["impressions"], 1),
               "ctr": (now["clicks"] / now["impressions"], base["clicks"] / base["impressions"], -1),
               "click_to_platform_lead": (now["platform_leads"] / now["clicks"], base["platform_leads"] / base["clicks"], -1),
               "platform_to_crm": (now["crm_leads"] / now["platform_leads"], base["crm_leads"] / base["platform_leads"], -1)}
    contributions, total = {}, 0.0
    usable = all(a > 0 and b > 0 for a, b, _ in factors.values()) if all(
        now[c] and base[c] for c in ("impressions", "clicks", "platform_leads", "crm_leads")) else False
    if usable:
        for name, (a, b, sign) in factors.items():
            contributions[name] = sign * math.log(a / b)
            total += contributions[name]
    split = ({name: {"log_points": round(v, 4), "share_of_change_pct": round(100 * v / total, 1) if total else None}
              for name, v in contributions.items()} if usable else None)

    mature = week <= _mature_until()
    drill = _drilldown(channel, week, base_index)
    return {
        "channel": channel, "week": str(week.date()),
        "baseline": f"mean of {len(base_index)} weeks {base_index[0].date()}..{base_index[-1].date()}",
        "now": stages_now, "baseline_values": stages_base,
        "cpl_crm_change_pct": _pct_change(stages_now["cpl_crm_eur"], stages_base["cpl_crm_eur"]),
        "cpl_split": split,
        "cpl_split_note": ("Shares sum to 100%: which step moved the cost per CRM lead. Positive = made "
                           "leads dearer." if usable else "Not computable: a funnel step is zero."),
        "sc1_mature": bool(mature),
        "sc1_note": None if mature else f"Leads from this week have had under {SC1_LAG_DAYS} days to book an SC1; SC1 figures are incomplete.",
        "drilldown": drill,
    }


def _drilldown(channel: str, week: pd.Timestamp, base_index: list) -> dict:
    """The campaigns (and, where tied, creatives) behind the channel's change."""
    n = len(base_index)
    campaigns = cd.campaign_weekly(channel)

    def movers(frame: pd.DataFrame, key: str, label: str) -> list:
        now = frame[frame["week"] == week].groupby(key)[cd.SPEND_SUMS + cd.FUNNEL_SUMS].sum()
        base = frame[frame["week"].isin(base_index)].groupby(key)[cd.SPEND_SUMS + cd.FUNNEL_SUMS].sum() / n
        both = now.join(base, lsuffix="_now", rsuffix="_base", how="outer").fillna(0.0)
        both["spend_delta"] = both["spend_now"] - both["spend_base"]
        out = []
        for name, row in both.reindex(both["spend_delta"].abs().sort_values(ascending=False).index).head(6).iterrows():
            out.append({label: name,
                        "spend_now": _r(row["spend_now"]), "spend_base_avg": _r(row["spend_base"]),
                        "ctr_now_pct": _r(100 * _ratio(row["clicks_now"], row["impressions_now"]), 3) if row["impressions_now"] else None,
                        "ctr_base_pct": _r(100 * _ratio(row["clicks_base"], row["impressions_base"]), 3) if row["impressions_base"] else None,
                        "platform_leads_now": _r(row["platform_leads_now"], 1),
                        "crm_leads_now": _r(row["crm_leads_now"], 1), "crm_leads_base_avg": _r(row["crm_leads_base"], 1)})
        return out

    result = {"campaigns_by_spend_change": movers(campaigns, "campaign", "campaign")}
    if channel in CREATIVE_CHANNELS:
        creatives = cd.creative_weekly()
        creatives = creatives[creatives["channel"] == channel]
        names = creatives.groupby("ad_key")["ad"].first()
        rows = movers(creatives, "ad_key", "ad")
        for row in rows:
            row["ad"] = names.get(row["ad"], row["ad"])
        result["creatives_by_spend_change"] = rows
    return result


# ------------------------------------------------------------- briefing

def fatigue(min_weekly_spend: float = 50, min_weeks: int = 4, ctr_drop: float = 0.25) -> dict:
    """Creatives whose CTR fell as they kept running: first 2 active weeks vs the last 2."""
    frame = cd.creative_weekly()
    spend = cd.spend_rows()
    last = last_complete_week(spend)
    frame = frame[(frame["week"] <= last) & (frame["spend"] >= min_weekly_spend) & (frame["impressions"] > 0)]
    flagged, checked = [], 0
    for (channel, _), group in frame.groupby(["channel", "ad_key"]):
        group = group.sort_values("week")
        if len(group) < min_weeks or group["week"].iloc[-1] < last - pd.Timedelta(days=7):
            continue
        checked += 1
        early, late = group.head(2)[cd.SPEND_SUMS].sum(), group.tail(2)[cd.SPEND_SUMS].sum()
        if late["impressions"] < 5000:
            continue
        ctr_early, ctr_late = early["clicks"] / early["impressions"], late["clicks"] / late["impressions"]
        if ctr_early and ctr_late / ctr_early - 1 <= -ctr_drop:
            flagged.append({
                "channel": channel, "ad": group["ad"].iloc[-1], "weeks_running": int(len(group)),
                "first_week": str(group["week"].iloc[0].date()),
                "ctr_first2_pct": _r(100 * ctr_early, 3), "ctr_last2_pct": _r(100 * ctr_late, 3),
                "ctr_change_pct": _pct_change(ctr_late, ctr_early),
                "cpm_change_pct": _pct_change(late["spend"] / late["impressions"], early["spend"] / early["impressions"]),
                "spend_last2_eur": _r(late["spend"]), "total_spend_eur": _r(group["spend"].sum())})
    flagged.sort(key=lambda item: -item["spend_last2_eur"])
    return {"rule": f"active >= {min_weeks} weeks at >= EUR {min_weekly_spend}/week, still running, "
                    f"CTR of the last 2 weeks >= {int(ctr_drop * 100)}% below the first 2",
            "through_week": str(last.date()), "creatives_checked": checked,
            "flagged": len(flagged), "top": flagged[:10],
            "note": "A falling CTR is consistent with fatigue; audience or placement shifts can do the same."}


def _mature_window(weeks: int) -> tuple[pd.Timestamp, pd.Timestamp]:
    end = _mature_until()
    end = end - pd.Timedelta(days=end.weekday())
    return end - pd.Timedelta(days=7 * (weeks - 1)), end


def quality_vs_volume(weeks: int = 6, min_spend: float = 500, min_leads: int = 20) -> dict:
    """Creatives whose cheap leads do not become SC1s, and the reverse (Meta/TikTok only)."""
    start, end = _mature_window(weeks)
    frame = cd.creative_weekly()
    frame = frame[frame["channel"].isin(CREATIVE_CHANNELS) & frame["week"].between(start, end)]
    sums = frame.groupby(["channel", "ad_key"]).agg(ad=("ad", "last"), **{c: (c, "sum") for c in cd.SPEND_SUMS + cd.FUNNEL_SUMS})
    sums = sums[(sums["spend"] >= min_spend) & (sums["crm_leads"] >= min_leads)]
    out = {"window": f"{start.date()}..{(end + pd.Timedelta(days=6)).date()} (SC1-mature)",
           "rule": f">= EUR {min_spend} and >= {min_leads} CRM leads in the window", "channels": {}}
    for channel, group in sums.groupby(level="channel"):
        cpl = group["spend"] / group["crm_leads"]
        cps = (group["spend"] / group["sc1_net"]).where(group["sc1_net"] > 0)
        med_cpl, med_cps = cpl.median(), cps.median()
        rows = []
        for (ch, key), row in group.iterrows():
            rows.append({"ad": row["ad"], "spend_eur": _r(row["spend"]), "crm_leads": _r(row["crm_leads"], 0),
                         "cpl_crm_eur": _r(cpl[(ch, key)]), "cost_per_sc1_eur": _r(cps[(ch, key)]),
                         "sc1_rate_pct": _r(100 * _ratio(row["sc1_net"], row["crm_leads"]), 1),
                         "low_prio_share_pct": _r(100 * _ratio(row["low_prio"], row["crm_leads"]), 1)})
        cheap_weak = [r for r in rows if r["cpl_crm_eur"] <= med_cpl and (r["cost_per_sc1_eur"] is None or r["cost_per_sc1_eur"] >= 1.25 * med_cps)]
        pricey_strong = [r for r in rows if r["cpl_crm_eur"] >= med_cpl and r["cost_per_sc1_eur"] is not None and r["cost_per_sc1_eur"] <= 0.8 * med_cps]
        out["channels"][channel] = {
            "creatives": len(rows), "median_cpl_crm_eur": _r(med_cpl), "median_cost_per_sc1_eur": _r(med_cps),
            "cheap_leads_weak_quality": sorted(cheap_weak, key=lambda r: -r["spend_eur"])[:5],
            "dear_leads_strong_quality": sorted(pricey_strong, key=lambda r: -r["spend_eur"])[:5]}
    return out


def attributes(weeks: int = 12, min_creatives: int = 5, min_leads: int = 100) -> dict:
    """Creative attributes (Ad Details) against CTR, CPL and SC1 rate, within Meta. Correlational."""
    start, end = _mature_window(weeks)
    frame = cd.creative_weekly()
    frame = frame[(frame["channel"] == "Meta") & frame["week"].between(start, end)]
    per_ad = frame.groupby("ad_key")[cd.SPEND_SUMS + cd.FUNNEL_SUMS].sum()
    detail = cd.details().set_index("ad_key")
    per_ad = per_ad.join(detail[list(ATTRIBUTES)], how="inner")
    out = {"window": f"{start.date()}..{(end + pd.Timedelta(days=6)).date()}", "creatives_with_attributes": len(per_ad),
           "rule": f"values with >= {min_creatives} creatives and >= {min_leads} CRM leads",
           "note": "Within one channel and period, but still correlational: the platform chooses who sees each ad.",
           "attributes": {}}
    for attribute in ATTRIBUTES:
        values = []
        for value, group in per_ad[per_ad[attribute].str.strip() != ""].groupby(attribute):
            leads = group["crm_leads"].sum()
            if len(group) < min_creatives or leads < min_leads:
                continue
            values.append({"value": value, "creatives": int(len(group)), "spend_eur": _r(group["spend"].sum()),
                           "ctr_pct": _r(100 * _ratio(group["clicks"].sum(), group["impressions"].sum()), 3),
                           "cpl_crm_eur": _r(_ratio(group["spend"].sum(), leads)),
                           "sc1_rate_pct": _r(100 * _ratio(group["sc1_net"].sum(), leads), 1),
                           "low_prio_share_pct": _r(100 * _ratio(group["low_prio"].sum(), leads), 1)})
        if len(values) >= 2:
            out["attributes"][attribute] = sorted(values, key=lambda v: -(v["sc1_rate_pct"] or 0))
    return out


def product_mix(weeks: int = 12) -> dict:
    """Share of spend vs share of CRM leads, SC1s and contracts, by product and by promo."""
    start = last_complete_week(cd.spend_rows()) - pd.Timedelta(days=7 * (weeks - 1))
    frame = cd.creative_weekly()
    # Only channels whose CRM leads tie to an ad: elsewhere a tagged ad's spend
    # would count while its leads could not, and its product would look dead.
    frame = frame[(frame["week"] >= start) & frame["channel"].isin(CREATIVE_CHANNELS)]
    detail = cd.details().set_index("ad_key")[["product", "promo"]]
    joined = frame.join(detail, on="ad_key", how="left").fillna({"product": "", "promo": ""})
    total_spend = frame["spend"].sum()
    known = joined[joined["product"].str.strip() != ""]
    out = {"window": f"{start.date()} onward", "channels": list(CREATIVE_CHANNELS),
           "spend_with_product_tag_pct": _r(100 * _ratio(known["spend"].sum(), total_spend), 1),
           "note": f"Contracts from the last {CONTRACT_LAG_WEEKS} weeks are still arriving; SC1 share is the earlier read.",
           "by": {}}
    for column in ("product", "promo"):
        tagged = joined[joined[column].str.strip() != ""]
        sums = tagged.groupby(column)[["spend", "crm_leads", "sc1_net", "contracts"]].sum()
        if sums.empty:
            continue
        shares = (100 * sums / sums.sum()).round(1)
        out["by"][column] = [{"value": name, "spend_eur": _r(sums.loc[name, "spend"]),
                              **{f"{c}_share_pct": shares.loc[name, c] for c in ("spend", "crm_leads", "sc1_net", "contracts")}}
                             for name in sums.sort_values("spend", ascending=False).index]
    return out


def channel_overview() -> dict:
    """Last complete week vs the 4 before it, per channel, whole-channel totals."""
    spend = cd.spend_rows()
    week = last_complete_week(spend)
    weekly = cd.channel_weekly()
    rows = []
    for channel, group in weekly.groupby("channel"):
        group = group.set_index("week").sort_index()
        if week not in group.index or not group.loc[week, "spend"]:
            continue
        base = group.loc[[w for w in group.index if w < week and w >= spend["week"].min()][-BASELINE_WEEKS:]]
        now, prior = _stages(group.loc[week]), _stages(base.sum())
        rows.append({"channel": channel, "spend_eur": now["spend_eur"],
                     "spend_change_pct": _pct_change(now["spend_eur"], base["spend"].mean()),
                     "crm_leads": now["crm_leads"], "crm_leads_change_pct": _pct_change(now["crm_leads"], base["crm_leads"].mean()),
                     "cpl_crm_eur": now["cpl_crm_eur"], "cpl_crm_change_pct": _pct_change(now["cpl_crm_eur"], prior["cpl_crm_eur"])})
    return {"week": str(week.date()), "baseline": f"mean of the {BASELINE_WEEKS} weeks before", "channels": rows}


def consultation_quality() -> dict | None:
    """The LOI agent's feed, trimmed to what the briefing needs. None if it never ran."""
    import loi_feed
    feed = loi_feed.cached()
    if not feed:
        return None
    return {"source": feed["folder"], "weekly": feed["weekly_quality"]["last_vs_prior_4w"],
            "through_week": feed["weekly_quality"]["through_week"],
            "calibration": {k: feed["calibration"][k] for k in ("ai_bands", "seller_fallback", "ai_order_holds",
                                                                "planner_outside_ci", "note")},
            "checks": feed["checks"],
            "reasons": ({k: feed["reasons"][k] for k in ("through_week", "primary_last_vs_prior_4w", "definitions", "note")}
                        if feed.get("reasons") else None),
            "last_week_in_the_calls": _digest_summary((feed.get("reasons") or {}).get("digest"))}


def _digest_summary(digest: dict | None) -> dict | None:
    """What was talked about last week: the movers, and the top items of each group."""
    if not digest or not digest.get("items"):
        return digest
    top = {}
    for item in digest["items"]:
        top.setdefault(item["group"], [])
        if len(top[item["group"]]) < 5:
            top[item["group"]].append({k: item[k] for k in ("name", "share_pct", "points")})
    return {"week": digest["week"], "consultations": digest["consultations"], "movers": digest["movers"],
            "top_by_group": top, "rule": digest.get("rule"), "note": digest.get("note")}


def briefing_sections() -> dict:
    sections = {"data": cd.meta(), "channel_overview": channel_overview(), "fatigue": fatigue(),
                "quality_vs_volume": quality_vs_volume(), "attributes": attributes(),
                "product_mix": product_mix(), "consultation_quality": consultation_quality(),
                "watchdog": creative_checks()}
    # The latest Planner plan with the agents' comments on it, so the briefing can
    # say what the week's findings mean for the plan on the table.
    try:
        import budget_pacing
        import loi_feed
        import plan_review
        pacing = budget_pacing.cached()
        sections["allocation_review"] = plan_review.review(plan_review.latest_plan(), sections,
                                                           loi_feed.cached() or {}, pacing)
        sections["budget_pacing"] = ({k: pacing.get(k) for k in ("week", "basis", "provisional", "channels", "flagged", "note")}
                                     if pacing else None)
    except Exception as error:                              # noqa: BLE001
        sections["allocation_review"] = {"status": "UNAVAILABLE", "detail": str(error)}
    return sections
