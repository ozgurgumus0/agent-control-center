"""The three agents that reason rather than compute: Analyst, Investigator, Context Scout.

Watchdog and Leak Tracer stay deterministic scripts -- an alarm must give the
same answer twice. These sit on top of them and only READ and WRITE REPORTS:

- Analyst: the weekly briefing. creative_insights computes every section
  (fatigue, quality vs volume, attributes, product mix, channel overview,
  checks); one Opus 5.5 call turns them into 3-5 findings.
- Investigator: Python gathers an incident's evidence (now including the
  funnel breakdown from the creative data); one Opus 5.5 call proposes a label.
- Context Scout: the only loop, because it must search and what it finds
  decides what it searches next; Sonnet 5, at most MAX_WEB_SEARCHES searches.

One call instead of a loop is where the cost went: a loop re-sends the whole
history on every step.

- Nothing here can change an incident's status, a policy, Planner or
  production. The Investigator *suggests* a label; a human sets it.
- Only aggregates leave this machine: spend, clicks and funnel COUNTS per
  channel, campaign or creative, and the incident files (aggregates too). The
  CRM export behind the Leak Tracer is lead-level personal data and nothing
  here can reach it.
- A run costs money, so it needs a key AND `llm_agents_enabled` in the control
  state, and is capped by MAX_COST_USD per run and MONTHLY_BUDGET_USD.
"""
from __future__ import annotations

import functools
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict

import pandas as pd

APP_ROOT = Path(__file__).resolve().parent
WORKSPACE_ROOT = APP_ROOT.parent
PROJECT_ROOT = Path(os.getenv("ATTRIBUTION_PROJECT_ROOT", str(WORKSPACE_ROOT / "attribution-project")))
INCIDENT_ROOT = PROJECT_ROOT / "outputs" / "monitoring" / "incidents"
RUN_ROOT = PROJECT_ROOT / "outputs" / "monitoring" / "runs"
ANALYST_ROOT = PROJECT_ROOT / "outputs" / "monitoring" / "analyst"
CONTEXT_ROOT = PROJECT_ROOT / "outputs" / "monitoring" / "context"
REGISTRY_PATH = PROJECT_ROOT / "config" / "event_registry.json"
REPLAY_QUEUE = sorted((PROJECT_ROOT / "outputs" / "monitoring").glob(
    "replay_review_*/owner_review_queue.csv"))

# The same extract the planner allocates from, so the
# Analyst reasons over the numbers the allocator sees.
ALLOCATOR_EXTRACT = Path(os.getenv(
    "ALLOCATOR_DATA",
    str(WORKSPACE_ROOT / "allocator" / "backtest_results" / "predictions_weekly.csv.gz")))

MODEL = "claude-opus-5-5"
# Context Scout searches and extracts dates: the cheaper model holds there.
MODELS = {"analyst": MODEL, "investigator": MODEL, "context": "claude-sonnet-5"}
# Opus 5.5 defaults to medium; set per agent explicitly. The Analyst and the
# Investigator are each one call over a prepared package, where medium holds.
EFFORT = {"analyst": "medium", "investigator": "medium", "context": "medium"}
MAX_TOKENS = 16000
MAX_ITERATIONS = 6
# The Analyst and the Investigator make ONE call each, so their cost is
# bounded by construction: at most SINGLE_CALL_MAX_INPUT_CHARS in (~3-4
# chars/token) and SINGLE_CALL_MAX_TOKENS out. 60k chars + 8k tokens is about
# $0.24 on Opus 5.5; the cap is the pre-run budget check, not a mid-run stop.
SINGLE_CALL_MAX_TOKENS = 8000
SINGLE_CALL_MAX_INPUT_CHARS = 60_000
INVESTIGATOR_MAX_TOKENS = SINGLE_CALL_MAX_TOKENS
INVESTIGATOR_MAX_INPUT_CHARS = SINGLE_CALL_MAX_INPUT_CHARS
MAX_COST_USD = {"analyst": 0.30, "investigator": 0.30, "context": 0.30}
# One ceiling across every agent. A run that could push the month past it is
# refused before any call is made.
MONTHLY_BUDGET_USD = 3.00
MAX_WEB_SEARCHES = 4
PRICE_WEB_SEARCH = 0.01          # $10 per 1,000 searches
# USD per million tokens. Cache reads 0.1x input, writes 1.25x input.
PRICES = {
    "claude-opus-5-5": {"input": 4.00, "output": 20.00, "cache_read": 0.20, "cache_write": 5.00},
    "claude-sonnet-5": {"input": 2.00, "output": 10.00, "cache_read": 0.20, "cache_write": 2.50},
}
PRICE = PRICES[MODEL]

INCIDENT_LABELS = ("CONFIRMED_OPERATIONAL_CHANGE", "EXPECTED_VARIATION",
                   "DATA_QUALITY_ISSUE", "UNRESOLVED")
CONFIDENCE = ("low", "medium", "high")

LLM_LOCK = threading.Lock()
STATUS: dict = {"running": None, "last_error": None, "last_finished_at_utc": None}

RULES = """Rules that override everything else:
- Every number you state must come from a tool result in this conversation. Never estimate or invent one.
- Write observations and hypotheses, not verdicts. Say "consistent with" or "suggests", never "caused by", unless a tool result shows the mechanism directly.
- Never recommend a budget, a spend level, a price or any money figure. Allocation is the company's decision; you describe what the data shows.
- Leads here are platform-reported leads. They carry no contract or lead-quality information; do not imply they do.
- Say plainly when data is missing, stale or does not cover the period in question.
- Write in English. Be brief. The reader is a marketing analyst who knows the channels."""

ANALYST_SYSTEM = f"""You are the Analyst agent of a local marketing-monitoring control center for a residential solar installer. You write the weekly briefing for the marketing team and management.

A script has already computed everything from the creative ad performance data (spend and clicks per ad, and the CRM funnel per creative: CRM leads, low-priority leads, SC1 booked, SC1 net, the sales rep's LOI rating, contracts). The sections are:
- channel_overview: last complete week vs the 4 before, per channel.
- fatigue: creatives whose CTR fell as they kept running.
- quality_vs_volume: creatives with cheap leads that do not become SC1s, and the reverse.
- attributes: creative attributes (hook, POV, setting, pace, format, person on screen) against CTR, CPL and SC1 rate, within Meta.
- product_mix: share of spend vs share of leads, SC1s and contracts, by product and promo.
- consultation_quality: from the LOI agent -- national consultation quality this week vs the 4 before (our AI LOI: readiness, hot share, negative share, cancellations, missing recordings), and whether the signing rates Planner assumes per LOI band match what those bands actually sign. Planner uses these rates inside EV, so a band outside its interval means EV is mis-stated for cells heavy in that band. Its "reasons" part is what customers say in consultations: weekly shares of three markers validated on signed contracts (financing discussed and "must ask partner" go with more signing; product/eligibility mismatch with less). Consultations that never discuss financing sign far less, so a falling financing share is worth a line. Its "last_week_in_the_calls" part says what the consultations talked about last week (topics, incentives, products, energy costs, energy brands) and which of those moved significantly vs the 4 weeks before; give the movers a finding when they matter, and remember energy_brands mixes the customer's current supplier with competitors.
- allocation_review: the latest Planner allocation plan per channel, with deterministic comments from the other agents (plan share vs last week's actual spend share, model CPL vs actual, data problems per channel, LOI). Open the briefing with what this week's findings mean for that plan: which parts rest on weak or stale inputs, where recent performance disagrees with the model. Describe; never say what the budget should be -- the plan is decided in Planner by people. If it is built on test data (data_source is not the production warehouse), say so in one line and do not treat it as a real plan.
- budget_pacing: budgets as set in Meta and Google Ads against what each campaign spent last week (not spending its budget, budget-limited, budget changed). It is a delivery check, not the planner's plan; if marked provisional, today's budgets stand in for last week's, so say so.
- watchdog: data integrity checks on the same data.
- previous_briefings: the last briefings you wrote (headline, findings, watch items). For every earlier watch item, fill follow_up: "held" if this week's data confirms it, "did_not_hold" if it contradicts it, "cannot_tell" if the data here does not cover it, with one sentence saying what the data shows. Do not repeat an earlier finding unless it changed; say what changed.

Read all of it and tell the reader what is HAPPENING and what deserves a look. Some sections will be empty or unremarkable; say so briefly instead of padding. Respect every note and rule printed in the data: SC1 figures for recent weeks are incomplete, contracts from the last 8 weeks are still arriving, attribute differences are correlational, per-creative analysis covers only channels whose CRM leads tie to an ad (mainly Meta and TikTok). The LOI figures are the sales rep's rating; only its top band (Caldo, LOI 7) carries any known signal.

How the report reads: 3 to 5 findings, most important first. Each finding is a short plain title, two or three sentences on what is going on and why it matters, and exactly ONE key number that carries it. Every other supporting number goes in the appendix as compact markdown tables. watch_items are specific things to re-check next week.

{RULES.replace("from a tool result in this conversation", "from the data in this message")}"""

REPORT_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string", "description": "One short sentence: the most important thing this week."},
        "findings": {"type": "array", "items": {
            "type": "object",
            "properties": {"title": {"type": "string"}, "story": {"type": "string"},
                           "key_number": {"type": "string"}},
            "required": ["title", "story", "key_number"], "additionalProperties": False}},
        "watch_items": {"type": "array", "items": {"type": "string"}},
        "follow_up": {"type": "array", "description": "One entry per watch item from the previous briefings. Empty if there were none.",
                      "items": {"type": "object",
                                "properties": {"item": {"type": "string"},
                                               "verdict": {"type": "string", "enum": ["held", "did_not_hold", "cannot_tell"]},
                                               "note": {"type": "string"}},
                                "required": ["item", "verdict", "note"], "additionalProperties": False}},
        "appendix_markdown": {"type": "string"},
    },
    "required": ["headline", "findings", "watch_items", "follow_up", "appendix_markdown"],
    "additionalProperties": False,
}

INVESTIGATOR_SYSTEM = f"""You are the Incident Investigator agent of a local marketing-monitoring control center for a residential solar installer. The deterministic Watchdog opened an incident and a script gathered everything relevant into the evidence below: the incident record and its evidence package, the daily series around the event date for the flagged channel and the totals, the events already registered near that date, and the latest Watchdog run. Your job is to work out what most plausibly happened and to say how sure you are.

Test more than one explanation: a deliberate operational change (budget or campaign change), normal variation, or a data problem. Look at what moved together with the flagged metric, whether it persisted, and what the campaign drilldown in the evidence package shows. You suggest a label; a human decides.

{RULES.replace("from a tool result in this conversation", "from the evidence in this message")}"""

HYPOTHESIS_SCHEMA = {
    "type": "object",
    "properties": {
        "hypothesis": {"type": "string",
                       "description": "The most plausible explanation, in two or three sentences."},
        "suggested_label": {"type": "string", "enum": list(INCIDENT_LABELS)},
        "confidence": {"type": "string", "enum": list(CONFIDENCE)},
        "supporting_evidence": {"type": "array", "items": {"type": "string"},
                                "description": "Facts from the evidence, each with its numbers."},
        "contradicting_evidence": {"type": "array", "items": {"type": "string"},
                                   "description": "Facts against it, or alternatives not ruled out."},
        "what_would_confirm": {"type": "array", "items": {"type": "string"},
                               "description": "What a human should check to confirm it."},
    },
    "required": ["hypothesis", "suggested_label", "confidence", "supporting_evidence",
                 "contradicting_evidence", "what_would_confirm"],
    "additionalProperties": False,
}


CONTEXT_SYSTEM = f"""You are the Context agent of a local marketing-monitoring control center for a company that sells residential solar, batteries and heat pumps in Italy. The Watchdog flags unusual days in leads and spend, but it only knows internal events. Your job is to find EXTERNAL, dated events that could plausibly have moved Italian residential solar demand or lead flow: government incentives and tax bonuses (changes, openings, deadlines, rate cuts), energy price shocks and the news around them, regulation, national holidays and long weekends, and major competitor campaigns.

Use the web search tool, sparingly: you have at most {MAX_WEB_SEARCHES} searches. Start from the unexplained anomaly dates (replay_cases) and the events already registered (known_events), so you search where it matters and do not re-find what is known. Prefer primary sources (government, GSE, Agenzia delle Entrate, ARERA) and reputable Italian news. Every event must have a real source URL you actually saw in a search result and a specific date. Do not guess dates. If you find nothing solid for a period, say so rather than padding the list.

When you are done, call submit_events exactly once. A human approves each event before it enters the registry.

{RULES}"""


class CandidateEvent(TypedDict):
    title: str
    start_date: str
    end_date: str
    event_type: str
    scope: str
    expected_effect: str
    source_url: str
    source_title: str
    reason: str


CONTEXT_EVENT_TYPES = ("government_incentive", "regulation", "energy_price", "holiday",
                       "competitor", "news_other")


# --------------------------------------------------------------------------- data

_CACHE: dict = {}


def load_allocator() -> pd.DataFrame:
    """One row per (date, Channel, Region), real actuals only. Cached on mtime."""
    key = (str(ALLOCATOR_EXTRACT), ALLOCATOR_EXTRACT.stat().st_mtime)
    if _CACHE.get("key") == key:
        return _CACHE["frame"]
    frame = pd.read_csv(ALLOCATOR_EXTRACT)
    # The file repeats the same actuals once per model variant.
    if "variant" in frame.columns:
        frame = frame[frame["variant"] == "base"]
    frame = frame.drop_duplicates(subset=["date", "Channel", "Region"])
    # 'Youtube'/'YouTube': collapse to the most frequent spelling, as Planner does.
    for column in ("Channel", "Region"):
        canonical: dict = {}
        for name in frame[column].value_counts().index:
            canonical.setdefault(str(name).casefold(), name)
        frame[column] = frame[column].map(lambda v: canonical.get(str(v).casefold(), v))
    frame = frame.assign(date=pd.to_datetime(frame["date"]))
    frame = frame[["date", "Channel", "Region", "Spend", "actual_leads", "base_pred"]]
    _CACHE.update(key=key, frame=frame)
    return frame


def _filter(frame: pd.DataFrame, channel: str = "", region: str = "") -> pd.DataFrame:
    if channel:
        frame = frame[frame["Channel"].str.casefold() == channel.casefold()]
    if region:
        frame = frame[frame["Region"].str.casefold() == region.casefold()]
    return frame


def _metrics(group: pd.DataFrame) -> dict:
    spend, leads, pred = (float(group["Spend"].sum()), float(group["actual_leads"].sum()),
                          float(group["base_pred"].sum()))
    return {"spend_eur": round(spend, 2), "leads": round(leads, 1),
            "cpl_eur": round(spend / leads, 2) if leads else None,
            "model_pred_leads": round(pred, 1),
            "model_bias_pct": round((pred / leads - 1) * 100, 1) if leads else None}


def _pct(new, old):
    return round((new / old - 1) * 100, 1) if new is not None and old else None


def dataset_overview() -> str:
    """What the allocator extract covers: dates, gaps, and each channel's active span and totals."""
    frame = load_allocator()
    calendar = pd.date_range(frame["date"].min(), frame["date"].max())
    missing = calendar.difference(pd.DatetimeIndex(frame["date"].unique()))
    active = frame[frame["Spend"] > 0]
    channels = {}
    for channel, group in frame.groupby("Channel"):
        spent = active[active["Channel"] == channel]
        channels[channel] = {
            **_metrics(group),
            "first_active": str(spent["date"].min().date()) if len(spent) else None,
            "last_active": str(spent["date"].max().date()) if len(spent) else None,
            "regions": int(group["Region"].nunique()),
        }
    return json.dumps({
        "source": "allocator backtest extract (same data Planner allocates from)",
        "start": str(frame["date"].min().date()), "end": str(frame["date"].max().date()),
        "missing_dates": [str(day.date()) for day in missing],
        "regions": sorted(frame["Region"].unique().tolist()),
        "channels": channels,
    })


def period_comparison(group_by: str = "channel", window_days: int = 28, channel: str = "",
                      region: str = "", top: int = 15) -> str:
    """Compare the last window_days with the window before it.

    Args:
        group_by: One of total, channel, region, cell (cell = channel x region).
        window_days: Length of each window in days, counted back from the last date (7-91).
        channel: Optional filter to one channel, e.g. Meta.
        region: Optional filter to one region, e.g. Lazio.
        top: For region/cell, how many groups to return, largest recent spend first (max 40).
    """
    keys = {"total": [], "channel": ["Channel"], "region": ["Region"],
            "cell": ["Channel", "Region"]}
    if group_by not in keys:
        return f"Error: group_by must be one of {', '.join(keys)}."
    window_days = max(7, min(int(window_days), 91))
    frame = _filter(load_allocator(), channel, region)
    if frame.empty:
        return "Error: no rows match that channel/region."
    end = frame["date"].max()
    last = frame[frame["date"] > end - pd.Timedelta(days=window_days)]
    prior = frame[(frame["date"] <= end - pd.Timedelta(days=window_days))
                  & (frame["date"] > end - pd.Timedelta(days=2 * window_days))]
    rows = []
    groups = (last.groupby(keys[group_by]) if keys[group_by] else [((), last)])
    for name, group in groups:
        name = name if isinstance(name, tuple) else (name,)
        match = prior
        for column, value in zip(keys[group_by], name):
            match = match[match[column] == value]
        now, before = _metrics(group), _metrics(match)
        rows.append({"group": " / ".join(map(str, name)) or "total", "last": now, "prior": before,
                     "spend_change_pct": _pct(now["spend_eur"], before["spend_eur"]),
                     "leads_change_pct": _pct(now["leads"], before["leads"]),
                     "cpl_change_pct": _pct(now["cpl_eur"], before["cpl_eur"])})
    rows.sort(key=lambda row: row["last"]["spend_eur"], reverse=True)
    return json.dumps({
        "last_window": f"{(end - pd.Timedelta(days=window_days - 1)).date()}..{end.date()}",
        "prior_window": f"{(end - pd.Timedelta(days=2 * window_days - 1)).date()}.."
                        f"{(end - pd.Timedelta(days=window_days)).date()}",
        "rows": rows[:max(1, min(int(top), 40))], "groups_total": len(rows)})


def time_series(freq: str = "W", channel: str = "", region: str = "", last_n: int = 12) -> str:
    """Spend, leads, CPL and model-predicted leads over time.

    Args:
        freq: D for daily, W for weekly (weeks end on the extract's last weekday).
        channel: Optional filter to one channel.
        region: Optional filter to one region.
        last_n: Number of most recent periods to return (max 60).
    """
    if freq not in {"D", "W"}:
        return "Error: freq must be D or W."
    frame = _filter(load_allocator(), channel, region)
    if frame.empty:
        return "Error: no rows match that channel/region."
    rule = "D" if freq == "D" else "W-" + frame["date"].max().strftime("%a").upper()
    rows = []
    for period, group in frame.set_index("date").groupby(pd.Grouper(freq=rule)):
        rows.append({"period_end": str(period.date()), "days_with_data": int(group.index.nunique()),
                     **_metrics(group)})
    return json.dumps({"freq": freq, "channel": channel or "all", "region": region or "all",
                       "rows": rows[-max(1, min(int(last_n), 60)):]})


def watchdog_findings() -> str:
    """The latest deterministic Watchdog run and the incidents it keeps open."""
    runs = sorted(RUN_ROOT.glob("*/run_manifest.json"), key=lambda p: p.stat().st_mtime)
    latest = {}
    if runs:
        manifest = json.loads(runs[-1].read_text(encoding="utf-8"))
        report = runs[-1].parent / "watchdog.json"
        watchdog = json.loads(report.read_text(encoding="utf-8")) if report.exists() else {}
        latest = {"run_id": manifest.get("run_id"), "status": manifest.get("status"),
                  "analysis_as_of": manifest.get("analysis_as_of"),
                  "failed_checks": [c for c in watchdog.get("checks", []) if not c["passed"]],
                  "anomalies": watchdog.get("anomalies", [])}
    incidents = [
        {k: item.get(k) for k in ("incident_id", "kind", "key", "event_date", "status")}
        for item in _incidents() if item.get("status") not in {"RESOLVED", "DISMISSED"}]
    return json.dumps({"latest_run": latest, "open_incidents": incidents})


def _incidents() -> list[dict]:
    items = []
    for path in INCIDENT_ROOT.glob("*/incident.json"):
        try:
            items.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return items


def _incident_dir(incident_id: str) -> Path | None:
    if not incident_id or any(ch in incident_id for ch in "/\\") or ".." in incident_id:
        return None
    folder = INCIDENT_ROOT / incident_id
    return folder if (folder / "incident.json").is_file() else None


def get_incident(incident_id: str) -> str:
    """The incident record and every evidence package built for it.

    Args:
        incident_id: The incident's id, as listed by watchdog_findings.
    """
    folder = _incident_dir(incident_id)
    if folder is None:
        return "Error: unknown incident id."
    incident = json.loads((folder / "incident.json").read_text(encoding="utf-8"))
    packages = {path.name: json.loads(path.read_text(encoding="utf-8"))
                for path in sorted(folder.glob("investigation_*.json"))}
    return json.dumps({"incident": incident, "evidence_packages": packages})


def _incident_source(incident_id: str) -> Path | None:
    """The measurement table the incident was detected on, via its last run."""
    folder = _incident_dir(incident_id)
    if folder is None:
        return None
    incident = json.loads((folder / "incident.json").read_text(encoding="utf-8"))
    for observation in reversed(incident.get("observations", [])):
        manifest = RUN_ROOT / str(observation.get("run_id")) / "run_manifest.json"
        if manifest.is_file():
            source = json.loads(manifest.read_text(encoding="utf-8"))["sources"]["measurement_csv"]["path"]
            return Path(source)
    return None


def incident_source_series(incident_id: str, columns: list[str], days_before: int = 35,
                           days_after: int = 0) -> str:
    """Daily values from the measurement table the incident was detected on, around its date.

    Args:
        incident_id: The incident's id.
        columns: Column names to return, e.g. ["spend_eur_google", "dashboard_leads_google"]. Call with an empty list to get the available columns.
        days_before: Days before the event date to include (max 120).
        days_after: Days after the event date to include (max 30).
    """
    source = _incident_source(incident_id)
    if source is None or not source.is_file():
        return "Error: the incident's source table cannot be found."
    table = pd.read_csv(source)
    # Aggregate columns only. The table holds no lead-level data, but keep the
    # tool to the column families the watchdog itself reads.
    allowed = [c for c in table.columns if c == "date" or c.startswith(
        ("total_", "dashboard_", "spend_eur_", "media_platform_", "allocator_"))]
    if not columns:
        return json.dumps({"source": source.parent.name, "columns": allowed})
    unknown = [c for c in columns if c not in allowed]
    if unknown:
        return f"Error: unknown columns {unknown}. Call with columns=[] to list them."
    incident = json.loads((INCIDENT_ROOT / incident_id / "incident.json").read_text(encoding="utf-8"))
    event = pd.Timestamp(incident["event_date"])
    table["date"] = pd.to_datetime(table["date"])
    window = table[(table["date"] >= event - pd.Timedelta(days=min(int(days_before), 120)))
                   & (table["date"] <= event + pd.Timedelta(days=min(int(days_after), 30)))]
    window = window[["date", *columns]].assign(
        weekday=window["date"].dt.strftime("%a"), date=window["date"].dt.date.astype(str))
    return json.dumps({"source": source.parent.name, "event_date": incident["event_date"],
                       "rows": json.loads(window.to_json(orient="records"))})


# ------------------------------------------------------------------------ agent run

def known_events() -> str:
    """The events already in the registry, so you do not search for them again."""
    registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    return json.dumps([{k: event.get(k) for k in ("id", "title", "event_type", "start_date",
                                                   "end_date", "evidence_status")}
                       for event in registry.get("events", [])])


def replay_cases() -> str:
    """Historical anomaly days the Watchdog flagged, and which of them no known event explains."""
    if not REPLAY_QUEUE:
        return "Error: no replay review queue found."
    queue = pd.read_csv(REPLAY_QUEUE[-1])
    keep = ["detection_date", "metric", "observed", "same_weekday_median", "known_event_context"]
    rows = queue[[c for c in keep if c in queue.columns]].fillna("")
    return json.dumps({"source": REPLAY_QUEUE[-1].parent.name,
                       "cases": json.loads(rows.to_json(orient="records"))})


def api_key() -> str | None:
    if os.getenv("ANTHROPIC_API_KEY"):
        return os.environ["ANTHROPIC_API_KEY"]
    env = APP_ROOT / ".env"
    if env.is_file():
        for line in env.read_text(encoding="utf-8").splitlines():
            name, _, value = line.partition("=")
            if name.strip() == "ANTHROPIC_API_KEY" and value.strip():
                return value.strip().strip('"').strip("'")
    return None


def _cost(usage, model: str = MODEL) -> float:
    price = PRICES[model]

    def get(name):
        return float(getattr(usage, name, 0) or 0)
    searches = float(getattr(getattr(usage, "server_tool_use", None), "web_search_requests", 0) or 0)
    return ((get("input_tokens") * price["input"] + get("output_tokens") * price["output"]
             + get("cache_read_input_tokens") * price["cache_read"]
             + get("cache_creation_input_tokens") * price["cache_write"]) / 1_000_000
            + searches * PRICE_WEB_SEARCH)


def _records() -> list[Path]:
    return [*ANALYST_ROOT.glob("*/report.json"), *INCIDENT_ROOT.glob("*/llm_hypothesis_*.json"),
            *CONTEXT_ROOT.glob("*/candidates.json")]


def spent_this_month() -> float:
    """Sum of recorded run costs this calendar month (UTC). Samples cost nothing."""
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    total = 0.0
    for path in _records():
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not record.get("sample") and str(record.get("generated_at_utc", "")).startswith(month):
            total += float(record.get("cost_usd") or 0)
    return round(total, 4)


def _tools(kind: str, submitted: dict, calls: list) -> list:
    from anthropic import beta_tool

    def logged(function):
        @functools.wraps(function)
        def wrapper(*args, **kwargs):
            calls.append({"tool": function.__name__, "args": kwargs})
            try:
                return function(*args, **kwargs)
            except Exception as error:                      # noqa: BLE001
                # Back to the model as a tool error, not a crashed run.
                return f"Error: {type(error).__name__}: {error}"
        return beta_tool(wrapper)

    def submit_events(events: list[CandidateEvent]) -> str:
        """Submit the candidate external events. Call exactly once, at the end.

        Args:
            events: The events found, each with: title; start_date and end_date as YYYY-MM-DD (end_date may equal start_date); event_type, one of government_incentive, regulation, energy_price, holiday, competitor, news_other; scope, e.g. "national" or a region name; expected_effect, one of demand_up, demand_down, unclear; source_url and source_title of a page you saw; reason, one sentence on why it could move residential solar leads. An empty list is a valid answer.
        """
        cleaned = []
        for index, event in enumerate(events):
            event = dict(event)
            try:
                start = pd.Timestamp(event["start_date"])
                end = pd.Timestamp(event.get("end_date") or event["start_date"])
            except (ValueError, KeyError):
                return f"Error: event {index} needs start_date/end_date as YYYY-MM-DD."
            if start > end:
                return f"Error: event {index} ends before it starts."
            if event.get("event_type") not in CONTEXT_EVENT_TYPES:
                return f"Error: event {index} event_type must be one of {', '.join(CONTEXT_EVENT_TYPES)}."
            if not str(event.get("source_url", "")).startswith(("http://", "https://")):
                return f"Error: event {index} needs a source_url you saw."
            event.update(start_date=start.date().isoformat(), end_date=end.date().isoformat(),
                         review=None)
            cleaned.append(event)
        submitted.update(events=cleaned)
        return "Events stored. You are done."

    # Only Context Scout still loops: it has to search, and what it finds decides
    # what it searches next. Its web searches are capped at MAX_WEB_SEARCHES.
    search = {"type": "web_search_20260209", "name": "web_search",
              "max_uses": MAX_WEB_SEARCHES,
              "user_location": {"type": "approximate", "country": "IT"}}
    return [logged(f) for f in (replay_cases, known_events)] + [search, beta_tool(submit_events)]


def investigator_evidence(incident_id: str, days_before: int = 35, days_after: int = 14) -> dict:
    """Everything the Investigator needs, gathered in Python so the model is called once.

    These are the reads the agent used to make itself through tools, one model
    round trip each: the incident and its evidence package, the daily series
    for the flagged channel plus the totals, the registered events near the
    date and the latest Watchdog run. Same read-only functions, same
    aggregate-only column families.
    """
    incident = json.loads(get_incident(incident_id))
    event_date = pd.Timestamp(incident["incident"]["event_date"])
    available = json.loads(incident_source_series(incident_id, []))["columns"]
    key = str(incident["incident"].get("key", ""))
    channel = key.rsplit("_", 1)[-1] if "_" in key else ""
    columns = [c for c in available if c != "date" and (
        (channel and c.endswith("_" + channel))
        or c in {"total_leads", "total_paid_spend_eur", "dashboard_nonorganic_leads"})]
    series = json.loads(incident_source_series(incident_id, columns, days_before, days_after))
    nearby = [event for event in json.loads(known_events())
              if event.get("start_date") and abs(
                  (pd.Timestamp(event["start_date"]) - event_date).days) <= 45]
    evidence = {"incident": incident["incident"],
                "evidence_packages": incident["evidence_packages"],
                "daily_series": series,
                "registered_events_within_45_days": nearby,
                "watchdog": json.loads(watchdog_findings())}
    # Consultation quality from the LOI agent (national; the file has no recent channel).
    try:
        import loi_feed
        feed = loi_feed.cached()
        if feed:
            evidence["consultation_quality"] = {"weeks": feed["weekly_quality"]["weeks"][-6:],
                                                "note": feed["weekly_quality"]["note"]}
    except Exception as error:                              # noqa: BLE001
        evidence["consultation_quality"] = {"error": f"LOI feed unavailable: {error}"}
    # Which step of the funnel moved, from the creative data -- when the flagged
    # metric names a channel that data knows and the event week is covered.
    creative_channel = CREATIVE_CHANNEL_BY_SUFFIX.get(channel)
    if creative_channel:
        try:
            import creative_insights
            evidence["funnel_breakdown"] = creative_insights.funnel_breakdown(creative_channel, event_date)
        except Exception as error:                          # noqa: BLE001
            evidence["funnel_breakdown"] = {"error": f"Creative data unavailable: {error}"}
    return evidence


CREATIVE_CHANNEL_BY_SUFFIX = {"google": "Google Search", "meta": "Meta", "outbrain": "Outbrain",
                              "pmax": "PMax", "tiktok": "TikTok", "youtube": "Youtube",
                              "display": "Display"}


def _single_call(kind: str, system: str, content: str, schema: dict, client) -> tuple:
    """One call, structured output. Returns (output, cost, stop_reason, meta)."""
    if len(content) > SINGLE_CALL_MAX_INPUT_CHARS:
        raise RuntimeError(f"{kind} input is {len(content):,} chars, over the "
                           f"{SINGLE_CALL_MAX_INPUT_CHARS:,} the cost bound assumes.")
    response = client.messages.create(
        model=MODELS[kind], max_tokens=SINGLE_CALL_MAX_TOKENS, system=system,
        messages=[{"role": "user", "content": content}],
        output_config={"effort": EFFORT[kind], "format": {"type": "json_schema", "schema": schema}})
    meta = {"input_chars": len(content)}
    output: dict = {}
    if response.stop_reason == "max_tokens":
        meta["error"] = "The answer hit the output limit."
    elif response.stop_reason != "refusal":
        text = next((b.text for b in response.content if b.type == "text"), "")
        try:
            output = json.loads(text)
        except json.JSONDecodeError:
            meta["error"] = "Response was not valid JSON."
    return output, _cost(response.usage, MODELS[kind]), response.stop_reason, meta


def _run_investigator(incident_id: str, client) -> tuple:
    evidence = json.dumps(investigator_evidence(incident_id), default=str)
    output, cost, stop, meta = _single_call(
        "investigator", INVESTIGATOR_SYSTEM,
        f"Investigate incident {incident_id}.\n\n<evidence>\n{evidence}\n</evidence>",
        HYPOTHESIS_SCHEMA, client)
    if output and (output.get("suggested_label") not in INCIDENT_LABELS
                   or output.get("confidence") not in CONFIDENCE):
        meta["error"] = "Label or confidence outside the allowed values."
        output = {}
    return output, cost, stop, meta


BRIEFING_MEMORY = 2       # earlier briefings the Analyst sees


def previous_briefings(limit: int = BRIEFING_MEMORY) -> list[dict]:
    """The last real briefings, trimmed to what a follow-up needs (layout samples excluded)."""
    found = []
    for path in sorted(ANALYST_ROOT.glob("*/report.json"), key=_mtime, reverse=True):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if record.get("sample") or record.get("status") != "COMPLETED":
            continue
        out = record.get("output") or {}
        found.append({"written": str(record.get("generated_at_utc", ""))[:10], "headline": out.get("headline"),
                      "findings": [{"title": f.get("title"), "key_number": f.get("key_number")} for f in out.get("findings", [])],
                      "watch_items": out.get("watch_items", [])})
        if len(found) >= limit:
            break
    return found


def _run_analyst(client, sections: dict | None = None) -> tuple:
    """The weekly briefing: the creative sections in, 3-5 findings out."""
    if sections is None:
        import creative_insights
        sections = creative_insights.briefing_sections()
    sections = {**sections, "previous_briefings": previous_briefings()}
    data = json.dumps(sections, default=str)
    output, cost, stop, meta = _single_call(
        "analyst", ANALYST_SYSTEM,
        f"Write this week's briefing.\n\n<data>\n{data}\n</data>", REPORT_SCHEMA, client)
    if output and not 3 <= len(output.get("findings", [])) <= 5:
        meta["error"] = f"Expected 3 to 5 findings, got {len(output.get('findings', []))}."
        output = {}
    meta["data_as_of"] = (sections.get("data") or {}).get("fetched_at_utc")
    return output, cost, stop, meta


def run_agent(kind: str, incident_id: str | None = None, client=None) -> dict:
    """Run one agent to completion and write its output. Returns the stored record."""
    if kind not in {"analyst", "investigator", "context"}:
        raise ValueError("kind must be analyst, investigator or context")
    if kind == "investigator" and _incident_dir(incident_id or "") is None:
        raise ValueError("investigator needs a valid incident_id")
    spent = spent_this_month()
    if spent + MAX_COST_USD[kind] > MONTHLY_BUDGET_USD:
        raise RuntimeError(f"Monthly LLM budget: ${spent:.2f} of ${MONTHLY_BUDGET_USD:.2f} spent; "
                           f"a {kind} run may cost up to ${MAX_COST_USD[kind]:.2f}.")
    if client is None:
        key = api_key()
        if not key:
            raise RuntimeError("No ANTHROPIC_API_KEY (environment or .env).")
        import anthropic
        client = anthropic.Anthropic(api_key=key)

    if kind in {"investigator", "analyst"}:
        submitted, cost, stop, meta = (_run_investigator(incident_id, client) if kind == "investigator"
                                       else _run_analyst(client))
        status = ("COMPLETED" if submitted else "REFUSED" if stop == "refusal" else "NO_OUTPUT")
        return _store(kind, incident_id, status, 1, cost, [], submitted, meta)

    submitted: dict = {}
    calls: list = []
    system, prompt = CONTEXT_SYSTEM, (
        "Find external events for 2026-02-01 to 2026-09-15. Start with replay_cases and "
        "known_events. Finish with submit_events.")

    tools = _tools(kind, submitted, calls)
    messages = [{"role": "user", "content": prompt}]
    cost, turns, stop, capped, restarts = 0.0, 0, None, False, 0
    # The Python runner ends silently on pause_turn (long server-tool turns such
    # as web search), so the history is mirrored and the runner restarted.
    while True:
        runner = client.beta.messages.tool_runner(
            model=MODELS[kind], max_tokens=MAX_TOKENS, system=system, tools=tools,
            messages=messages, output_config={"effort": EFFORT[kind]},
            max_iterations=MAX_ITERATIONS)
        for message in runner:
            turns += 1
            cost += _cost(message.usage, MODELS[kind])
            stop = message.stop_reason
            messages.append({"role": "assistant", "content": message.content})
            response = runner.generate_tool_call_response()
            if response is not None:
                messages.append(response)
            if submitted or stop == "refusal":
                break
            if cost > MAX_COST_USD[kind]:
                capped = True
                break
        if submitted or capped or stop != "pause_turn" or restarts >= 3:
            break
        restarts += 1

    status = ("COMPLETED" if submitted else "REFUSED" if stop == "refusal"
              else "COST_CAPPED" if capped else "NO_OUTPUT")
    return _store(kind, incident_id, status, turns, cost, calls, submitted, {})


def _store(kind: str, incident_id: str | None, status: str, turns: int, cost: float,
           calls: list, submitted: dict, meta: dict) -> dict:
    now = datetime.now(timezone.utc)
    record = {"agent": kind, "status": status, "model": MODELS[kind], "effort": EFFORT[kind],
              "generated_at_utc": now.isoformat(), "incident_id": incident_id,
              "turns": turns, "cost_usd": round(cost, 4), "tool_calls": calls,
              "output": submitted, "suggestion_only": True, **meta}
    stamp = now.strftime("%Y-%m-%d_%H%M%S")
    if kind == "analyst":
        path = ANALYST_ROOT / stamp / "report.json"
    elif kind == "context":
        path = CONTEXT_ROOT / stamp / "candidates.json"
    else:
        path = INCIDENT_ROOT / incident_id / f"llm_hypothesis_{stamp}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    record["path"] = str(path)
    return record


def run_in_background(kind: str, incident_id: str | None = None) -> None:
    """One LLM run at a time; the UI polls STATUS through /api/state."""
    if not LLM_LOCK.acquire(blocking=False):
        raise RuntimeError(f"An LLM agent is already running: {STATUS['running']}")
    STATUS.update(running=kind, last_error=None)

    def work():
        try:
            run_agent(kind, incident_id)
        except Exception as error:                          # noqa: BLE001
            STATUS["last_error"] = f"{type(error).__name__}: {error}"[:500]
        finally:
            STATUS.update(running=None, last_finished_at_utc=datetime.now(timezone.utc).isoformat())
            LLM_LOCK.release()
    threading.Thread(target=work, daemon=True).start()


def _mtime(path: Path) -> float:
    # Newest by write time, not by name: a name like "SAMPLE_..." would
    # otherwise sort after every dated result and shadow them.
    return path.stat().st_mtime


def review_hypothesis(incident_id: str, action: str, label: str | None = None) -> dict:
    """Record the human decision on the latest hypothesis. Returns that record."""
    folder = _incident_dir(incident_id)
    found = sorted(folder.glob("llm_hypothesis_*.json"), key=_mtime) if folder else []
    if not found:
        raise FileNotFoundError("No hypothesis for this incident")
    record = json.loads(found[-1].read_text(encoding="utf-8"))
    record["review"] = {"action": action, "label": label,
                        "at_utc": datetime.now(timezone.utc).isoformat()}
    found[-1].write_text(json.dumps(record, indent=2), encoding="utf-8")
    return record


def latest_context() -> dict | None:
    found = sorted(CONTEXT_ROOT.glob("*/candidates.json"), key=_mtime)
    if not found:
        return None
    record = json.loads(found[-1].read_text(encoding="utf-8"))
    record["run"] = found[-1].parent.name
    return record


def review_context_event(run: str, index: int, action: str) -> dict:
    """Approve (append to the event registry) or reject one candidate event."""
    if not run or any(ch in run for ch in "/\\") or ".." in run:
        raise ValueError("Invalid run")
    path = CONTEXT_ROOT / run / "candidates.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    if record.get("sample"):
        raise ValueError("This is a layout sample, not an Opus result; it cannot enter the registry.")
    events = record.get("output", {}).get("events", [])
    if not 0 <= index < len(events):
        raise ValueError("Unknown event index")
    event = events[index]
    if event.get("review"):
        raise ValueError("This event was already reviewed")
    if action not in {"approve", "reject"}:
        raise ValueError("action must be approve or reject")
    if action == "approve":
        registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
        backup = CONTEXT_ROOT / f"event_registry_before_{run}.json"
        if not backup.exists():
            backup.write_text(json.dumps(registry, indent=2), encoding="utf-8")
        slug = "".join(ch if ch.isalnum() else "_" for ch in event["title"].lower())[:40].strip("_")
        registry["events"].append({
            "id": f"ext_{event['start_date']}_{slug}",
            "title": event["title"],
            "event_type": f"external_{event['event_type']}",
            "start_date": event["start_date"],
            "end_date": event["end_date"],
            "evidence_status": "EXTERNAL_SOURCE_CITED",
            "natural_intervention_candidate": False,
            "scope": event.get("scope", "national"),
            "sources": [event["source_url"]],
            "reason": f"{event.get('reason', '')} Expected effect: {event.get('expected_effect', 'unclear')}. "
                      f"Proposed by {MODEL} context agent ({run}), approved by a human.",
        })
        tmp = REGISTRY_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(registry, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, REGISTRY_PATH)
    event["review"] = {"action": action, "at_utc": datetime.now(timezone.utc).isoformat()}
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    return event


def latest_analyst_report() -> dict | None:
    reports = sorted(ANALYST_ROOT.glob("*/report.json"), key=_mtime)
    return json.loads(reports[-1].read_text(encoding="utf-8")) if reports else None


def latest_hypothesis(incident_id: str) -> dict | None:
    folder = _incident_dir(incident_id)
    found = sorted(folder.glob("llm_hypothesis_*.json"), key=_mtime) if folder else []
    return json.loads(found[-1].read_text(encoding="utf-8")) if found else None
