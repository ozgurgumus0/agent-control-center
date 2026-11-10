"""LOI agent: what the consultation scores say, as aggregates the other agents can use.

Source: whichever is newer of
- the dispatch-sheet snapshot the LOI MVP's daily scoring run saves
  (`gsheet_score_<date>/sheet_before.json`, the sheet as it stood before that
  run wrote), and
- the newest `LOI Totali <n>_filled.xlsx` (only when someone sends a new
  Totali workbook).
The sheet carries no "Seller LOI", so the seller-fallback check always comes
from the newest Totali workbook (mature calls only, so its age barely matters).
The same scores reach Planner through its database, where ev_score.sql
turns closing_readiness into a signing probability per lead.

PERSONAL DATA: the file is one row per consultation and carries lead ids,
host ids and Zoom links. Only the columns in USECOLS are kept -- none of
them identifies a person (a JSON snapshot cannot be read column by column, so
the other columns are dropped right after parsing) -- and nothing below
returns or stores a row: every
output is a count, a share or a rate over a group, and groups smaller than
MIN_GROUP are suppressed. Keep it that way; lead-level work belongs in
internal tools.

What the file can and cannot say (measured 2026-09-30):
- scoring is current (scored calls to the day before the file), so national
  weekly quality and the calibration check are live;
- channel and region come from the CRM export and stop where it stops (end
  of June at the time of writing): the per-channel view is historical until a
  fresh export or database access arrives. Planner itself is not affected --
  it joins leads to the live lead table in the data warehouse.
"""
from __future__ import annotations

import json
import math
import os
import re
from pathlib import Path

import pandas as pd

APP_ROOT = Path(__file__).resolve().parent
WORKSPACE_ROOT = APP_ROOT.parent
LOI_OUTPUTS = Path(os.getenv("LOI_OUTPUTS_DIR", str(WORKSPACE_ROOT / "loi-scoring" / "outputs")))
PLANNER_EV_SQL = Path(os.getenv("PLANNER_EV_SQL", str(Path(os.getenv("PLANNER_ROOT", str(WORKSPACE_ROOT / "planner")))
                                                  / "planner" / "sql" / "ev_score.sql")))
CACHE_PATH = APP_ROOT / "runtime" / "loi_cache" / "loi_feed.json"
# Weekly marker shares written by the LOI scoring project's `python -m src.export_reason_aggregates`
# (literal markers over the local transcripts; aggregates only).
REASON_DIR = LOI_OUTPUTS / "reason_aggregates"

SNAPSHOT_GLOB = "gsheet_score_*/sheet_before.json"
USECOLS = ["startTime", "channel", "region", "loi_match_status", "loi_band", "closing_readiness",
           "Seller LOI", "Firmato_Post_SC1_Gross", "Firmato_Post_SC1_Net"]
NEGATIVE_BANDS = {"Non idoneo", "Non interessato"}
BANDS = ["Caldo", "Calduccio", "Tiepido", "Freddo", "Non idoneo", "Non interessato"]
CONVERSION_DAYS = 40            # ev_score.sql P_LOI_CONVERSION_DAYS: when a call's outcome counts as known
MIN_GROUP = 20                  # no rate is shown over fewer calls
HOT_READINESS = 70

CHANNELS = {"facebook": "Meta", "fb": "Meta", "ig": "Meta", "instagram": "Meta",
            "search ads": "Google Search", "google": "Google Search", "google ads": "Google Search",
            "performancemax": "PMax", "outbrain": "Outbrain", "display": "Display",
            "youtube": "Youtube", "organic": "Organic", "organic_brand": "Organic",
            "bing ads": "Bing"}


def newest_workbook() -> Path | None:
    files = list(LOI_OUTPUTS.glob("loi_totali_*/LOI Totali *_filled.xlsx"))
    return max(files, key=lambda p: p.stat().st_mtime) if files else None


def newest_file() -> Path | None:
    """The fresher of the newest Totali workbook and the newest daily sheet snapshot."""
    files = list(LOI_OUTPUTS.glob(SNAPSHOT_GLOB))
    workbook = newest_workbook()
    files += [workbook] if workbook else []
    return max(files, key=lambda p: p.stat().st_mtime) if files else None


def _read(path: Path) -> pd.DataFrame:
    if path.suffix == ".json":
        rows = json.loads(path.read_text(encoding="utf-8"))
        header = rows[0]
        keep = [c for c in USECOLS if c in header]
        index = [header.index(c) for c in keep]
        frame = pd.DataFrame([[row[i] if i < len(row) else None for i in index] for row in rows[1:]], columns=keep)
        del rows
        frame = frame.replace("", None)
        frame["closing_readiness"] = pd.to_numeric(frame["closing_readiness"], errors="coerce")
        for column in USECOLS:
            if column not in frame:
                frame[column] = float("nan")
        return frame
    return pd.read_excel(path, usecols=USECOLS)


_MEMO: dict = {}


def load(path: Path | None = None, memo: bool = True) -> pd.DataFrame:
    """The non-identifying columns only, one row per meeting. Kept in memory, never written."""
    path = path or newest_file()
    if path is None:
        raise FileNotFoundError(f"No LOI filled workbook under {LOI_OUTPUTS}")
    key = (str(path), path.stat().st_mtime)
    if key not in _MEMO:
        if memo:
            _MEMO.clear()
        frame = _read(path)
        frame["start"] = pd.to_datetime(frame["startTime"], errors="coerce", utc=True, format="ISO8601").dt.tz_localize(None)
        frame["week"] = (frame["start"] - pd.to_timedelta(frame["start"].dt.weekday, unit="D")).dt.normalize()
        frame["channel_norm"] = frame["channel"].map(lambda v: CHANNELS.get(str(v).strip().lower(), "Other")
                                                     if pd.notna(v) else None)
        frame["scored"] = frame["loi_match_status"].eq("scored")
        frame["negative"] = frame["loi_band"].isin(NEGATIVE_BANDS)
        for column in ("Firmato_Post_SC1_Gross", "Firmato_Post_SC1_Net"):
            frame[column] = frame[column].map({"Si": 1.0, "No": 0.0})
        _MEMO[key] = frame.drop(columns=["startTime"])
    return _MEMO[key]


def planner_mapping(sql_path: Path | None = None) -> dict:
    """The signing probabilities the planner uses, read from ev_score.sql itself (not a copy)."""
    sql_path = sql_path or PLANNER_EV_SQL
    text = sql_path.read_text(encoding="utf-8")
    block = text[text.index("loi_per_lead AS ("):]
    ai = block[:block.index("UNION ALL")]
    seller = block[block.index("UNION ALL"):]
    cuts = [(int(c), float(p)) for c, p in re.findall(r"WHEN fa\.cr < (\d+) THEN ([0-9.]+)", ai)]
    top = float(re.search(r"ELSE\s+([0-9.]+)", ai).group(1))
    neg = float(re.search(r"WHEN fa\.neg = 1 THEN ([0-9.]+)", ai).group(1))
    seller_map = {int(k): float(v) for k, v in re.findall(r"WHEN (\d) THEN ([0-9.]+)", seller[:600])}
    bands, low = [], None
    for cut, prob in cuts:
        bands.append({"band": f"<{cut}" if low is None else f"{low}-{cut - 1}", "low": low, "high": cut, "planner_p": prob})
        low = cut
    bands.append({"band": f">={low}", "low": low, "high": None, "planner_p": top})
    return {"ai_bands": bands, "negative_p": neg, "seller": seller_map, "source": str(sql_path.name)}


def _wilson(k: float, n: float, z: float = 1.96) -> tuple[float, float]:
    if not n:
        return (0.0, 1.0)
    p = k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return (max(0.0, centre - half), min(1.0, centre + half))


def _r(value, digits=1):
    return None if value is None or (isinstance(value, float) and math.isnan(value)) else round(value, digits)


def _rate_row(label: str, group: pd.DataFrame, outcome: str, assumed: float | None) -> dict:
    n, k = len(group), float(group[outcome].sum())
    if n < MIN_GROUP:
        return {"band": label, "calls": n, "signed": None, "rate_pct": None, "planner_pct": _r(100 * assumed, 1) if assumed is not None else None,
                "note": f"fewer than {MIN_GROUP} mature calls"}
    low, high = _wilson(k, n)
    row = {"band": label, "calls": n, "signed": int(k), "rate_pct": _r(100 * k / n, 1),
           "ci95_pct": [_r(100 * low, 1), _r(100 * high, 1)]}
    if assumed is not None:
        row["planner_pct"] = _r(100 * assumed, 1)
        row["planner_inside_ci"] = low <= assumed <= high
    return row


def calibration(frame: pd.DataFrame, outcome: str = "Firmato_Post_SC1_Net",
                seller_frame: pd.DataFrame | None = None) -> dict:
    """Does closing_readiness still sign in the order, and at the rates, Planner assumes?"""
    mapping = planner_mapping()
    as_of = frame["start"].max()
    mature = frame[frame["scored"] & frame[outcome].notna() & (frame["start"] <= as_of - pd.Timedelta(days=CONVERSION_DAYS))]
    rows = [_rate_row("Non idoneo / Non interessato", mature[mature["negative"]], outcome, mapping["negative_p"])]
    positive = mature[~mature["negative"] & mature["closing_readiness"].notna()]
    for band in mapping["ai_bands"]:
        mask = pd.Series(True, index=positive.index)
        if band["low"] is not None:
            mask &= positive["closing_readiness"] >= band["low"]
        if band["high"] is not None:
            mask &= positive["closing_readiness"] < band["high"]
        rows.append(_rate_row(f"readiness {band['band']}", positive[mask], outcome, band["planner_p"]))
    rates = [r["rate_pct"] for r in rows[1:] if r["rate_pct"] is not None]
    ordered = all(a <= b + 0.5 for a, b in zip(rates, rates[1:]))
    seller = []
    sf = frame if seller_frame is None else seller_frame
    seller_as_of = sf["start"].max()
    seller_mature = sf[sf["Seller LOI"].notna() & sf[outcome].notna()
                       & (sf["start"] <= seller_as_of - pd.Timedelta(days=CONVERSION_DAYS))]
    for level, prob in sorted(mapping["seller"].items()):
        seller.append(_rate_row(f"seller LOI {level}", seller_mature[seller_mature["Seller LOI"] == level], outcome, prob))
    off = [r["band"] for r in rows + seller if r.get("planner_inside_ci") is False]
    return {"outcome": outcome, "mature_until": str((as_of - pd.Timedelta(days=CONVERSION_DAYS)).date()),
            "mature_scored_calls": int(len(mature)), "ai_bands": rows, "ai_order_holds": ordered,
            "seller_fallback": seller, "planner_outside_ci": off, "planner_source": mapping["source"],
            "note": (f"Calls at least {CONVERSION_DAYS} days old, signed after a held SC1. the planner's rate "
                     "outside the 95% interval means EV for that band is likely mis-stated.")}


def _week_stats(group: pd.DataFrame) -> dict:
    scored = group[group["scored"]]
    held = group[~group["loi_match_status"].isin(["cancelled", "scheduled_future"])]
    out = {"meetings": int(len(group)), "cancelled_pct": _r(100 * group["loi_match_status"].eq("cancelled").mean()),
           "no_recording_pct": _r(100 * held["loi_match_status"].eq("no_recording_found").mean()) if len(held) else None,
           "scored": int(len(scored))}
    if len(scored) >= MIN_GROUP:
        ready = scored["closing_readiness"]
        out.update({"mean_readiness": _r(ready.mean()), "hot_pct": _r(100 * (ready >= HOT_READINESS).mean()),
                    "negative_pct": _r(100 * scored["negative"].mean()),
                    "bands_pct": {b: _r(100 * scored["loi_band"].eq(b).mean()) for b in BANDS}})
    return out


def weekly_quality(frame: pd.DataFrame, weeks: int = 8) -> dict:
    """National consultation quality per week, last complete week vs the 4 before."""
    as_of = frame.loc[frame["scored"], "start"].max().normalize()
    last = as_of - pd.Timedelta(days=as_of.weekday())
    last = last if as_of.weekday() == 6 else last - pd.Timedelta(days=7)
    rows = []
    for week in pd.date_range(end=last, periods=weeks, freq="7D"):
        rows.append({"week": str(week.date()), **_week_stats(frame[frame["week"] == week])})
    now, base = rows[-1], [r for r in rows[-5:-1] if r.get("mean_readiness") is not None]
    change = {}
    for key in ("mean_readiness", "hot_pct", "negative_pct", "cancelled_pct", "no_recording_pct"):
        if now.get(key) is not None and base:
            prior = sum(r[key] for r in base if r.get(key) is not None) / len(base)
            change[key] = {"now": now[key], "prior_4w": _r(prior), "points": _r(now[key] - prior)}
    return {"through_week": str(last.date()), "weeks": rows, "last_vs_prior_4w": change,
            "note": "All channels together: the file carries no channel for recent calls."}


def by_channel(frame: pd.DataFrame) -> dict:
    """Consultation quality and signing per channel, over the calls that carry a channel (historical)."""
    tagged = frame[frame["scored"] & frame["channel_norm"].notna()]
    if tagged.empty:
        return {"channels": [], "window": None}
    as_of = frame["start"].max()
    rows = []
    for channel, group in tagged.groupby("channel_norm"):
        if len(group) < MIN_GROUP:
            continue
        mature = group[group["start"] <= as_of - pd.Timedelta(days=CONVERSION_DAYS)]
        rows.append({"channel": channel, "scored_calls": int(len(group)),
                     "mean_readiness": _r(group["closing_readiness"].mean()),
                     "hot_pct": _r(100 * (group["closing_readiness"] >= HOT_READINESS).mean()),
                     "negative_pct": _r(100 * group["negative"].mean()),
                     "signed_net_pct": _r(100 * mature["Firmato_Post_SC1_Net"].mean()) if len(mature) >= MIN_GROUP else None})
    rows.sort(key=lambda r: -r["scored_calls"])
    return {"window": f"{tagged['start'].min().date()}..{tagged['start'].max().date()}",
            "channel_tags_end": str(tagged["start"].max().date()), "channels": rows,
            "note": "Historical: channel comes from the CRM export and stops where it stops."}


def reasons() -> dict | None:
    """The newest weekly reason feed, as written by the LOI pipeline. Already aggregated."""
    files = sorted(REASON_DIR.glob("reason_aggregates_*.json"))
    if not files:
        return None
    feed = json.loads(files[-1].read_text(encoding="utf-8"))
    feed["file"] = files[-1].name
    return feed


def _seller_frame(frame: pd.DataFrame) -> pd.DataFrame | None:
    """The sheet snapshot has no Seller LOI: take it from the newest Totali workbook."""
    if frame["Seller LOI"].notna().any():
        return None
    workbook = newest_workbook()
    return load(workbook, memo=False) if workbook else None


def _source_label(path: Path) -> str:
    return f"daily sheet snapshot {path.parent.name}" if path.suffix == ".json" else path.parent.name


def checks(frame: pd.DataFrame, path: Path, today: pd.Timestamp | None = None,
           calib: dict | None = None) -> list[dict]:
    today = (today or pd.Timestamp.now()).normalize()
    latest = frame.loc[frame["scored"], "start"].max()
    age = (today - latest.normalize()).days
    tags_end = frame.loc[frame["scored"] & frame["channel_norm"].notna(), "start"].max()
    tag_age = (today - tags_end.normalize()).days if pd.notna(tags_end) else None
    recent = frame[(frame["start"] > today - pd.Timedelta(days=28)) & (frame["start"] <= today)]
    held = recent[~recent["loi_match_status"].isin(["cancelled", "scheduled_future"])]
    unscored = held[~held["scored"]]
    calib = calib or calibration(frame, seller_frame=_seller_frame(frame))
    out = [
        {"id": "loi_freshness", "title": "Consultation scores are current",
         "status": "warn" if age > 3 else "ok",
         "detail": f"Latest scored call {latest.date()} ({age}d before today), from {_source_label(path)}."},
        {"id": "loi_channel_tags", "title": "Scored calls carry a channel and region",
         "status": "warn" if tag_age is None or tag_age > 45 else "ok",
         "detail": (f"Channel/region stop at {tags_end.date()} ({tag_age}d ago): they come from the CRM export. "
                    "Per-channel LOI is historical until a fresh export or database access."
                    if tag_age is not None else "No scored call carries a channel.")},
        {"id": "loi_unscored_share", "title": "Held meetings get scored",
         "status": "warn" if len(held) and len(unscored) / len(held) > 0.5 else "ok",
         "detail": (f"Last 28 days: {len(unscored)} of {len(held)} held meetings unscored "
                    f"({100 * len(unscored) / max(len(held), 1):.0f}%), mostly "
                    f"{unscored['loi_match_status'].value_counts().index[0] if len(unscored) else 'none'}.")},
        {"id": "loi_calibration", "title": "the planner's signing rates match what LOI bands actually sign",
         "status": "warn" if calib["planner_outside_ci"] or not calib["ai_order_holds"] else "ok",
         "detail": ((f"Outside the 95% interval: {', '.join(calib['planner_outside_ci'])}. " if calib["planner_outside_ci"] else "All Planner rates inside the observed interval. ")
                    + ("Readiness still signs in order." if calib["ai_order_holds"] else "Readiness bands no longer sign in order."))},
    ]
    reason = reasons()
    reason_age = (today - pd.Timestamp(reason["generated_at_utc"][:10])).days if reason else None
    out.append({"id": "loi_reasons_fresh", "title": "Reason feed (what customers say) is recent",
                "status": "warn" if reason_age is None or reason_age > 8 else "ok",
                "detail": (f"{reason['file']}: weeks through {reason['through_week']} ({reason_age}d old). "
                           "Refresh weekly: python -m src.export_reason_aggregates" if reason
                           else "Not produced yet: in the LOI scoring project run python -m src.export_reason_aggregates")})
    return out


def feed(path: Path | None = None, today: pd.Timestamp | None = None) -> dict:
    """Everything the other agents use from LOI. Aggregates only."""
    path = path or newest_file()
    frame = load(path)
    calib = calibration(frame, seller_frame=_seller_frame(frame))
    items = checks(frame, path, today, calib)
    return {"file": path.name, "folder": path.parent.name, "source": _source_label(path),
            "latest_scored_call": str(frame.loc[frame["scored"], "start"].max().date()),
            "file_modified": pd.Timestamp.fromtimestamp(path.stat().st_mtime).isoformat(timespec="minutes"),
            "status": "WARN" if any(c["status"] == "warn" for c in items) else "HEALTHY",
            "checks": items, "weekly_quality": weekly_quality(frame), "calibration": calib,
            "by_channel": by_channel(frame), "reasons": reasons()}


def refresh() -> dict:
    result = feed()
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(result, default=str), encoding="utf-8")
    tmp.replace(CACHE_PATH)
    return result


def cached() -> dict | None:
    return json.loads(CACHE_PATH.read_text(encoding="utf-8")) if CACHE_PATH.is_file() else None
