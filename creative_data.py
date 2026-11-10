"""Creative ad performance: the agents' second data source, next to the allocator extract.

Reads the production creative ad performance Google Sheet the Creative
Dashboard reads, with the dashboard's own rules ported from
the dashboard's source so the agents and the dashboard cannot
disagree about what a number is:

- channel names through normChannel() (New_Data says "Google", main_metrics
  says "Google Search": both become "Google Search");
- the per-channel campaign exclusions (other business lines), CAMPAIGN_EXCLUDE;
- New_Data matched to an ad by EXACT UTM Content == ad name on the same
  channel (TikTok: the UTM ends with the ad name). Everything else -- blank
  UTM (all of PMax, most of Outbrain), Display's suffix tags -- is counted at
  CHANNEL level only. The dashboard's Outbrain cohort split and Display suffix
  guess are deliberately not ported: they are estimates, and an agent must
  not present an estimate as a per-creative fact.

Read-only. Only aggregates are fetched: spend/clicks per ad per day, and lead,
SC1, LOI and contract COUNTS per creative per day. The per-lead
Lead_Contract_Cohort tab is not read.

The live tabs start 2026-06-01 (older rows sit in the archive spreadsheet);
that is enough for the weekly questions asked here and keeps one source.
Fetching is manual (refresh()); everything else reads the local cache.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

APP_ROOT = Path(__file__).resolve().parent
ADS_ROOT = Path(os.getenv("ADS_AUTOMATION_ROOT", str(APP_ROOT.parent / "ads-automation")))
CACHE_DIR = APP_ROOT / "runtime" / "creative_cache"

TABS = {"spend": "main_metrics", "spend_outbrain": "main_metrics_outbrain",
        "funnel": "New_Data", "details": "Ad Details"}

# Other business lines that share ad names with residential campaigns:
# (channel, lower-case campaign-name fragment) pairs. Set CAMPAIGN_EXCLUDE_RULES
# to a JSON list of [channel, fragment] pairs; the default is an example.
CAMPAIGN_EXCLUDE = tuple(tuple(pair) for pair in json.loads(os.getenv(
    "CAMPAIGN_EXCLUDE_RULES",
    '[["PMax", "otherbu"], ["TikTok", "otherbu"], ["Google Search", "otherbu"]]')))
# Not marketing at all: recruiting campaigns run from the same ad account.
# Reported by the watchdog, then kept out of every marketing metric.
NON_MARKETING = re.compile(r"(?:^|[_\s-])(?:hr|recruit\w*|hiring|lavora)(?:[_\s-]|$)", re.I)

FUNNEL_COLUMNS = {
    "Lead Cohort": "crm_leads", "Low Prio Cohort": "low_prio", "Called Cohort": "called",
    "Reached & SC1 Scheduled": "sc1_booked", "SC1 Net Cohort": "sc1_net",
    "LOI 1": "loi1", "LOI 4": "loi4", "LOI 6": "loi6", "LOI 7": "loi7",
    "Contratti Firmati": "contracts",
}
SPEND_COLUMNS = {"Date": "date", "Campaign ID": "campaign_id", "Campaign name": "campaign", "Ad name": "ad", "Ad ID": "ad_id",
                 "Spend": "spend", "Impressions": "impressions", "Clicks": "clicks",
                 "Leads": "platform_leads", "Channel": "channel"}
DETAIL_COLUMNS = {"Ad Name": "ad", "Product Type": "product", "Channel": "channel",
                  "Media Type": "media_type", "Promo": "promo", "Hook category": "hook_category",
                  "POV": "pov", "Setting": "setting", "Edit pace": "edit_pace",
                  "Format & duration": "format", "Person on screen": "person_on_screen",
                  "Main message": "main_message"}


def norm_channel(value) -> str:
    """normChannel() from the dashboard."""
    c = str(value or "").strip().lower()
    mapping = {"meta": "Meta", "display": "Display", "youtube": "Youtube", "yt": "Youtube",
               "outbrain": "Outbrain", "google": "Google Search", "google search": "Google Search",
               "search": "Google Search", "pmax": "PMax", "performance max": "PMax",
               "performance_max": "PMax", "tiktok": "TikTok", "tik tok": "TikTok"}
    if c in mapping:
        return mapping[c]
    raw = str(value or "").strip()
    return raw[:1].upper() + raw[1:] if raw else ""


def excluded_campaign(channel: str, campaign) -> bool:
    name = str(campaign or "").strip().lower()
    return bool(name) and any(channel == ch and part in name for ch, part in CAMPAIGN_EXCLUDE)


def week_start(dates: pd.Series) -> pd.Series:
    """Monday of the ISO week, as a Timestamp."""
    return (dates - pd.to_timedelta(dates.dt.weekday, unit="D")).dt.normalize()


# ------------------------------------------------------------------------ fetch

def _sheet():
    import gspread
    from dotenv import dotenv_values
    env = {**dotenv_values(ADS_ROOT / ".env"), **os.environ}
    credentials = env.get("GOOGLE_SERVICE_ACCOUNT_FILE") or str(ADS_ROOT / "service_account.json")
    client = gspread.service_account(filename=credentials)
    return client.open_by_key(env["GOOGLE_SHEETS_ID_PRODUCTION"])


def _frame(values: list[list[str]], columns: dict) -> pd.DataFrame:
    header, rows = values[0], values[1:]
    index = {name: header.index(name) for name in columns if name in header}
    missing = [name for name in columns if name not in index]
    if missing:
        raise RuntimeError(f"Sheet is missing expected columns: {missing}")
    width = max(index.values()) + 1
    padded = [row + [""] * (width - len(row)) for row in rows]
    return pd.DataFrame({new: [row[index[old]] for row in padded] for old, new in columns.items()})


def refresh(sheet=None) -> dict:
    """Fetch the four tabs into the local cache. Manual only; returns the metadata."""
    sheet = sheet or _sheet()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    frames = {
        "spend": pd.concat([_frame(sheet.worksheet(TABS[key]).get_values(), SPEND_COLUMNS)
                            for key in ("spend", "spend_outbrain")], ignore_index=True),
        "funnel": _frame(sheet.worksheet(TABS["funnel"]).get_values(),
                         {"Canale": "channel", "UTM Campaign": "campaign", "UTM Content": "utm",
                          "Lead Created Date": "date", **FUNNEL_COLUMNS}),
        "details": _frame(sheet.worksheet(TABS["details"]).get_values(), DETAIL_COLUMNS),
    }
    meta = {"fetched_at_utc": datetime.now(timezone.utc).isoformat(), "tabs": {}}
    for name, frame in frames.items():
        frame.to_csv(CACHE_DIR / f"{name}.csv", index=False)
        dates = pd.to_datetime(frame["date"], errors="coerce") if "date" in frame else None
        meta["tabs"][name] = {"rows": len(frame),
                              "max_date": str(dates.max().date()) if dates is not None and dates.notna().any() else None}
    (CACHE_DIR / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    _MEMO.clear()
    return meta


def meta() -> dict | None:
    path = CACHE_DIR / "meta.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


# ------------------------------------------------------------------------- load

_MEMO: dict = {}


def _memo(function):
    """Cache a derived frame until the next refresh (keyed on the cache's fetch stamp)."""
    import functools

    @functools.wraps(function)
    def wrapper(*args):
        stamp = (meta() or {}).get("fetched_at_utc")
        key = (function.__name__, args, stamp)
        if key not in _MEMO:
            _MEMO[key] = function(*args)
        return _MEMO[key].copy()
    return wrapper


def _cached(name: str) -> pd.DataFrame:
    path = CACHE_DIR / f"{name}.csv"
    if not path.is_file():
        raise FileNotFoundError("No creative data cached yet: run creative_data.refresh().")
    key = (name, path.stat().st_mtime)
    if key not in _MEMO:
        _MEMO[key] = pd.read_csv(path, dtype=str, keep_default_na=False)
    return _MEMO[key]


def _numbers(frame: pd.DataFrame, columns) -> pd.DataFrame:
    for column in columns:
        frame[column] = pd.to_numeric(frame[column].str.replace(",", ".", regex=False),
                                      errors="coerce").fillna(0.0)
    return frame


def spend_rows(include_non_marketing: bool = False) -> pd.DataFrame:
    """One row per ad per day: spend, impressions, clicks, platform leads."""
    frame = _cached("spend").copy()
    frame["channel"] = frame["channel"].map(norm_channel)
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame = frame[frame["date"].notna() & (frame["channel"] != "")]
    frame = _numbers(frame, ["spend", "impressions", "clicks", "platform_leads"])
    keep = ~frame.apply(lambda r: excluded_campaign(r["channel"], r["campaign"]), axis=1)
    frame = frame[keep]
    frame["non_marketing"] = frame["campaign"].str.contains(NON_MARKETING)
    if not include_non_marketing:
        frame = frame[~frame["non_marketing"]]
    frame["ad_key"] = frame["ad"].str.strip().str.lower()
    if "campaign_id" not in frame:           # a cache fetched before the column was read
        frame["campaign_id"] = ""
    frame["week"] = week_start(frame["date"])
    return frame


def funnel_rows() -> pd.DataFrame:
    """One row per channel x campaign x UTM Content x lead day: the CRM funnel counts."""
    frame = _cached("funnel").copy()
    frame["channel"] = frame["channel"].map(norm_channel)
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame = frame[frame["date"].notna() & (frame["channel"] != "")]
    frame = _numbers(frame, list(FUNNEL_COLUMNS.values()))
    # _accumulateNewData: trailing '=' is an export artefact, the key is lower-case.
    frame["utm_key"] = frame["utm"].str.strip().str.replace(r"=+\s*$", "", regex=True).str.strip().str.lower()
    frame["week"] = week_start(frame["date"])
    return frame


def details() -> pd.DataFrame:
    frame = _cached("details").copy()
    frame = frame[frame["ad"].str.strip() != ""]
    frame["ad_key"] = frame["ad"].str.strip().str.lower()
    frame["channel"] = frame["channel"].map(norm_channel)
    return frame.drop_duplicates("ad_key")


def match_funnel_to_ads(spend: pd.DataFrame, funnel: pd.DataFrame) -> pd.DataFrame:
    """Give each funnel row the ad it belongs to, or '' when it can only count at channel level.

    Exact UTM == ad name on the same channel; TikTok's UTM carries a variant
    prefix and ENDS with the ad name (_lookupTikTokEndsWith in the dashboard).
    """
    ads_by_channel = spend.groupby("channel")["ad_key"].apply(lambda s: set(s[s != ""]))
    matched = []
    for channel, utm in zip(funnel["channel"], funnel["utm_key"]):
        ads = ads_by_channel.get(channel, set())
        if not utm:
            matched.append("")
        elif utm in ads:
            matched.append(utm)
        elif channel == "TikTok":
            hits = [ad for ad in ads if utm.endswith(ad)]
            matched.append(max(hits, key=len) if hits else "")
        else:
            matched.append("")
    return funnel.assign(ad_key=matched)


SPEND_SUMS = ["spend", "impressions", "clicks", "platform_leads"]
FUNNEL_SUMS = list(FUNNEL_COLUMNS.values())


@_memo
def channel_weekly() -> pd.DataFrame:
    """Channel x week: spend side and funnel side summed over the WHOLE channel."""
    spend = spend_rows().groupby(["channel", "week"], as_index=False)[SPEND_SUMS].sum()
    funnel = funnel_rows().groupby(["channel", "week"], as_index=False)[FUNNEL_SUMS].sum()
    return spend.merge(funnel, on=["channel", "week"], how="outer").fillna(0.0)


@_memo
def creative_weekly() -> pd.DataFrame:
    """Ad x week on the spend side, with the funnel counts of the rows matched to that ad."""
    spend = spend_rows()
    funnel = match_funnel_to_ads(spend, funnel_rows())
    by_ad = (spend[spend["ad_key"] != ""]
             .groupby(["channel", "ad_key", "week"], as_index=False)
             .agg(ad=("ad", "first"), campaign=("campaign", "first"),
                  **{column: (column, "sum") for column in SPEND_SUMS}))
    counts = (funnel[funnel["ad_key"] != ""]
              .groupby(["channel", "ad_key", "week"], as_index=False)[FUNNEL_SUMS].sum())
    merged = by_ad.merge(counts, on=["channel", "ad_key", "week"], how="left")
    merged[FUNNEL_SUMS] = merged[FUNNEL_SUMS].fillna(0.0)
    return merged


def campaign_key(value) -> str:
    """_newDataCampKey(): URL-decoded, trailing '=' dropped, lower-case."""
    from urllib.parse import unquote
    text = str(value or "").strip()
    if "%" in text:
        text = unquote(text)
    return re.sub(r"=+\s*$", "", text).strip().lower()


def campaign_loose_key(value) -> str:
    """_campLooseKey(): drops a '::<tag>' suffix, collapses spaces/underscores."""
    return re.sub(r"[\s_]+", "_", re.sub(r"::.*$", "", campaign_key(value))).strip()


@_memo
def campaign_weekly(channel: str) -> pd.DataFrame:
    """Campaign x week for one channel, funnel matched by UTM Campaign.

    Exact key first, then the loose key only when exactly one campaign on the
    channel claims it -- the dashboard's _campCohortIndex rule, so two
    campaigns differing only by '::PV' never merge.
    """
    spend = spend_rows()
    spend = spend[spend["channel"] == channel]
    by_campaign = spend.groupby(["campaign", "week"], as_index=False)[SPEND_SUMS].sum()
    names = by_campaign["campaign"].unique()
    exact = {campaign_key(n): n for n in names}
    loose: dict = {}
    for name in names:
        loose.setdefault(campaign_loose_key(name), []).append(name)
    funnel = funnel_rows()
    funnel = funnel[funnel["channel"] == channel].copy()

    def owner(value):
        key = campaign_key(value)
        if key in exact:
            return exact[key]
        claims = loose.get(campaign_loose_key(value), [])
        return claims[0] if len(claims) == 1 else ""
    funnel["campaign_match"] = funnel["campaign"].map(owner)
    counts = (funnel[funnel["campaign_match"] != ""]
              .groupby(["campaign_match", "week"], as_index=False)[FUNNEL_SUMS].sum()
              .rename(columns={"campaign_match": "campaign"}))
    merged = by_campaign.merge(counts, on=["campaign", "week"], how="left")
    merged[FUNNEL_SUMS] = merged[FUNNEL_SUMS].fillna(0.0)
    return merged


def funnel_coverage(since: pd.Timestamp) -> dict:
    """Per channel: share of CRM leads since `since` that could be tied to a specific ad."""
    spend = spend_rows()
    funnel = match_funnel_to_ads(spend, funnel_rows())
    recent = funnel[funnel["date"] >= since]
    out = {}
    for channel, group in recent.groupby("channel"):
        leads = float(group["crm_leads"].sum())
        tied = float(group.loc[group["ad_key"] != "", "crm_leads"].sum())
        out[channel] = {"crm_leads": round(leads), "tied_to_ad_pct": round(100 * tied / leads, 1) if leads else None}
    return out
