"""The agents' findings as MCP tools, so Claude Code / Claude Desktop can answer questions with them.

"Why did Outbrain's CPL jump?", "which Meta creatives are tiring?", "does the
plan agree with last week?" -- Claude calls these tools and answers from the
same numbers the control center shows. Nothing new is computed that the
agents do not already compute, and every tool is READ-ONLY:

- no tool writes a file, changes an incident, a policy, Planner or production;
- only aggregates come out: per channel, campaign, creative or week. No lead
  id, host, meeting, transcript or CRM row is reachable from here;
- tools read the control center's caches (refreshed by "Run agents now"), so
  each answer is as fresh as the last run -- `overview` says how fresh.

Run (an MCP client such as Claude Code starts it from its .mcp.json):
    python mcp_server.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_ROOT))

# Settings come from the environment; a local .env (see .env.example) fills it in.
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env")
except ImportError:
    pass

from mcp.server.mcpserver import MCPServer  # noqa: E402
from mcp.types import ToolAnnotations  # noqa: E402

import budget_pacing  # noqa: E402
import creative_data  # noqa: E402
import llm_agents  # noqa: E402
import loi_feed  # noqa: E402
import plan_review  # noqa: E402

SECTIONS_PATH = creative_data.CACHE_DIR / "briefing_sections.json"
READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)

INSTRUCTIONS = """Marketing agents (read-only). Rules for answering with these tools:
- Describe what the data shows; never recommend a budget, spend level or money figure. Allocation is decided by people in Planner.
- Call `overview` first when freshness matters: every tool reads the last agent run, and some sources are old (it says which).
- Leads in creative data: platform leads vs CRM leads are different things; say which you mean. SC1 figures for the last ~2 weeks and contracts for the last ~8 weeks are incomplete.
- LOI: our AI LOI (closing readiness) signs in order; the rep's LOI is weak. "energy brands" in the call digest mixes the customer's current supplier with competitors.
- Attribution: nothing here proves a channel causes leads; only an experiment can.
- The Planner plan may be built on test data (data_source is not the production warehouse): then it is not a real plan."""

server = MCPServer(name="marketing-agents", instructions=INSTRUCTIONS)


def _sections() -> dict:
    if not SECTIONS_PATH.is_file():
        return {}
    return json.loads(SECTIONS_PATH.read_text(encoding="utf-8"))


def _dump(value) -> str:
    return json.dumps(value, default=str, ensure_ascii=False)


def _missing(what: str) -> str:
    return _dump({"error": f"{what} not computed yet: press 'Run agents now' in the control center (http://127.0.0.1:8765)."})


@server.tool(annotations=READ_ONLY)
def overview() -> str:
    """How fresh each source is and the headline status of every agent. Call this first."""
    sections, loi = _sections(), loi_feed.cached() or {}
    meta = (sections.get("data") or {}).get("tabs") or {}
    incidents = [i for i in llm_agents._incidents() if i.get("status") not in {"RESOLVED", "DISMISSED"}]
    report = llm_agents.latest_analyst_report()
    return _dump({
        "creative_data": {"fetched_at_utc": (sections.get("data") or {}).get("fetched_at_utc"),
                          "spend_to": (meta.get("spend") or {}).get("max_date"),
                          "funnel_to": (meta.get("funnel") or {}).get("max_date"),
                          "checks_status": (sections.get("watchdog") or {}).get("status")},
        "loi": {"latest_scored_call": loi.get("latest_scored_call"), "status": loi.get("status"),
                "reason_feed_through": (loi.get("reasons") or {}).get("through_week")},
        "open_incidents": [{"id": i["incident_id"], "metric": i.get("key"), "date": i.get("event_date")} for i in incidents],
        "latest_briefing": None if not report or report.get("sample") else
                           {"written": str(report.get("generated_at_utc"))[:10], "headline": (report.get("output") or {}).get("headline")},
    })


@server.tool(annotations=READ_ONLY)
def allocation_plan() -> str:
    """The latest Planner allocation per channel, with the agents' comments on it (plan share vs actual, model CPL vs actual, data problems, LOI)."""
    return _dump(plan_review.review(plan_review.latest_plan(), _sections(), loi_feed.cached() or {}, budget_pacing.cached()))


@server.tool(annotations=READ_ONLY)
def budget_pacing_check() -> str:
    """Budgets as set in Meta and Google Ads vs what each campaign spent last week: under-delivering, budget-limited, budget changed."""
    return _dump(budget_pacing.cached() or {"error": "No budget snapshot yet: press 'Run agents now'."})


@server.tool(annotations=READ_ONLY)
def channel_overview() -> str:
    """Last complete week vs the 4 before, per channel: spend, CRM leads, cost per CRM lead."""
    return _dump(_sections().get("channel_overview") or {}) if _sections() else _missing("Creative data")


@server.tool(annotations=READ_ONLY)
def funnel_breakdown(channel: str, week: str = "") -> str:
    """Which funnel step moved a channel's cost per CRM lead (CPM, CTR, click-to-lead, platform-to-CRM), with the campaigns and creatives behind it.

    Args:
        channel: Meta, Google Search, Outbrain, PMax, TikTok, Youtube or Display.
        week: Monday of the week as YYYY-MM-DD; empty = last complete week.
    """
    import creative_insights
    try:
        return _dump(creative_insights.funnel_breakdown(channel, week or None))
    except FileNotFoundError:
        return _missing("Creative data")


@server.tool(annotations=READ_ONLY)
def creative_checks() -> str:
    """Data Watchdog checks on the creative data: freshness, tracking gaps, creatives spending without reaching the CRM, non-marketing spend, untagged creatives."""
    return _dump(_sections().get("watchdog") or {}) if _sections() else _missing("Creative data")


@server.tool(annotations=READ_ONLY)
def creative_fatigue() -> str:
    """Long-running creatives whose CTR fell as they kept running."""
    return _dump(_sections().get("fatigue") or {}) if _sections() else _missing("Creative data")


@server.tool(annotations=READ_ONLY)
def quality_vs_volume() -> str:
    """Meta/TikTok creatives with cheap leads that rarely reach an SC1, and dearer ones whose leads do."""
    return _dump(_sections().get("quality_vs_volume") or {}) if _sections() else _missing("Creative data")


@server.tool(annotations=READ_ONLY)
def creative_attributes() -> str:
    """Creative attributes (hook, POV, setting, pace, format, person on screen) against CTR, CPL and SC1 rate within Meta. Correlational."""
    return _dump(_sections().get("attributes") or {}) if _sections() else _missing("Creative data")


@server.tool(annotations=READ_ONLY)
def product_mix() -> str:
    """Share of spend vs share of CRM leads, SC1s and contracts, by product and promo (Meta/TikTok)."""
    return _dump(_sections().get("product_mix") or {}) if _sections() else _missing("Creative data")


@server.tool(annotations=READ_ONLY)
def loi_quality() -> str:
    """Consultation quality per week (our AI LOI: readiness, hot share, negative share), meeting flow (cancellations, missing recordings), the LOI checks, and the historical per-channel view."""
    loi = loi_feed.cached()
    if not loi:
        return _missing("LOI feed")
    return _dump({k: loi.get(k) for k in ("folder", "latest_scored_call", "status", "checks", "weekly_quality", "by_channel")})


@server.tool(annotations=READ_ONLY)
def loi_calibration() -> str:
    """Does each LOI band sign at the rate Planner assumes inside EV? Observed rate, 95% interval, Planner rate, per band."""
    loi = loi_feed.cached()
    return _dump((loi or {}).get("calibration")) if loi else _missing("LOI feed")


@server.tool(annotations=READ_ONLY)
def call_reasons() -> str:
    """What customers say in consultations: weekly shares of the validated markers, and last week's call digest (topics, incentives, products, energy costs, energy brands, movers)."""
    loi = loi_feed.cached()
    return _dump((loi or {}).get("reasons")) if loi and loi.get("reasons") else _missing("Reason feed")


@server.tool(annotations=READ_ONLY)
def incidents() -> str:
    """Open and recent incidents with the Investigator's latest suggestion (label, confidence, reasons for and against, what to check)."""
    out = []
    for item in llm_agents._incidents():
        hyp = llm_agents.latest_hypothesis(item.get("incident_id", ""))
        out.append({"id": item.get("incident_id"), "metric": item.get("key"), "date": item.get("event_date"),
                    "status": item.get("status"), "evidence": item.get("current_evidence"),
                    "suggestion": (hyp or {}).get("output") if hyp and not hyp.get("sample") else None,
                    "human_review": (hyp or {}).get("review")})
    return _dump(out)


@server.tool(annotations=READ_ONLY)
def latest_briefing() -> str:
    """The latest weekly briefing written by the Analyst, if one exists."""
    report = llm_agents.latest_analyst_report()
    if not report or report.get("sample") or report.get("status") != "COMPLETED":
        return _dump({"briefing": None, "note": "No briefing written yet (the LLM agents are switched off or have not run)."})
    return _dump({"written": str(report.get("generated_at_utc"))[:10], **(report.get("output") or {})})


if __name__ == "__main__":
    server.run("stdio")
