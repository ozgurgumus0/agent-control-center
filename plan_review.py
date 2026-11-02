"""The latest Planner allocation, and what the agents have to say about it. Deterministic, $0.

Planner decides the plan; this module only reads it (the Planner backend's own
GET endpoints) and sets it next to what the other agents know: how each
channel has actually been doing (creative data), data problems per channel
(creative checks), lead quality (LOI Agent), and whether the plan's inputs can
be trusted (data source, LOI rates inside EV, the recalibration comparison).

Every comment is a description with its source, never a budget instruction:
the plan stays a human decision in Planner.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path

PLANNER_API = os.getenv("PLANNER_API_URL", "http://127.0.0.1:8000")
TIMEOUT_S = 3
CACHE_S = 60

# A production run exported from the Planner app (Budget_Allocation_<ddmm>.xlsx).
# The local backend only holds test runs, so when an export is newer than the
# newest local run, the export is the plan that is actually being executed.
EXPORT_DIR = Path(os.getenv("PLANNER_EXPORT_DIR", str(Path(__file__).resolve().parent / "runtime" / "planner_exports")))
# The data_source value that marks a run built on real production data.
PRODUCTION_SOURCE = os.getenv("PLANNER_PRODUCTION_SOURCE", "warehouse")
EXPORT_GLOB = "Budget_Allocation_*.xlsx"

# Planner names Google Search "Google"; the creative data uses the dashboard's names.
CREATIVE_NAME = {"Google": "Google Search", "Meta": "Meta", "PMax": "PMax", "Outbrain": "Outbrain",
                 "Youtube": "Youtube", "YouTube": "Youtube", "Display": "Display", "TikTok": "TikTok"}

SHARE_SHIFT_PTS = 10        # plan share vs recent actual spend share
CPL_RISE_PCT = 25
CPL_FALL_PCT = -15
MODEL_GAP_PCT = 30          # Planner model CPL vs recent actual CRM CPL

_CACHE: dict = {}


def _get(path: str):
    with urllib.request.urlopen(f"{PLANNER_API}{path}", timeout=TIMEOUT_S) as response:
        return json.loads(response.read().decode("utf-8"))


def _rows(sheet) -> list:
    rows = [list(r) for r in sheet.iter_rows(values_only=True)]
    return rows[1:] if rows else []


def plan_from_export(path: Path) -> dict:
    """A Planner app export, in the same shape as GET /api/runs/{id}."""
    from openpyxl import load_workbook

    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        summary = [[r[0], r[1], r[2] if len(r) > 2 else None]
                   for r in _rows(workbook["Riepilogo Run"]) if r and r[0] is not None]
        voce = {str(r[0]).strip(): r[1] for r in summary}
        sheet = workbook["Per Canale"]
        header = [str(h) for h in next(sheet.iter_rows(values_only=True))]
        channel = [dict(zip(header, r)) for r in _rows(sheet) if r and r[0]]
    finally:
        workbook.close()
    computed = str(voce.get("Calcolata il") or "").replace(" UTC", "")
    try:
        created_at = datetime.strptime(computed, "%d/%m/%Y %H:%M").isoformat(timespec="minutes")
    except ValueError:
        created_at = datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="minutes")
    valid_for = voce.get("Perimetro / giorno_previsto")
    return {"id": voce.get("ID run"), "status": str(voce.get("Stato") or "").lower(),
            "valid_for": str(valid_for)[:10] if valid_for else None, "metodo": voce.get("metodo"),
            "created_at": created_at, "config": {"data_source": voce.get("Sorgente dati")},
            "summary": summary, "channel": channel, "source_file": path.name}


def latest_export() -> dict | None:
    files = sorted(EXPORT_DIR.glob(EXPORT_GLOB), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in files:
        try:
            return plan_from_export(path)
        except (KeyError, ValueError, OSError, StopIteration, zipfile.BadZipFile):
            continue                # an unreadable export must not hide the local runs
    return None


def latest_plan() -> dict:
    """The newest plan: a production export if it is newer than the local runs,
    else the newest non-rejected local run and its comparison twin. Cached for CACHE_S seconds."""
    if _CACHE.get("at", 0) > time.time() - CACHE_S:
        return _CACHE["value"]
    value = _latest_local()
    export = latest_export()
    local_run = value.get("run") if value.get("available") else None
    if export and (not local_run or export["created_at"] > str(local_run.get("created_at") or "")):
        local_runs = value.get("runs") or []
        value = {"available": True, "run": export, "comparison": None,
                 "runs": [{k: export.get(k) for k in ("id", "status", "valid_for", "metodo", "created_at")}] + local_runs[:4]}
    _CACHE.update(at=time.time(), value=value)
    return value


def _latest_local() -> dict:
    """The newest non-rejected run in the local Planner backend, its comparison twin if any."""
    try:
        runs = [r for r in _get("/api/runs") if r.get("status") != "rejected"]
        if not runs:
            value = {"available": True, "run": None}
        else:
            newest = max(runs, key=lambda r: r["created_at"])
            # Of a comparison pair, show the classic plan first, as Planner does.
            twin_id = newest.get("confronto_con")
            if twin_id and newest.get("metodo") != "classico":
                twin = next((r for r in runs if r["id"] == twin_id), None)
                if twin and twin.get("metodo") == "classico":
                    newest = twin
            run = _get(f"/api/runs/{newest['id']}")
            comparison = _get(f"/api/runs/{run['id']}/confronto") if run.get("confronto_con") else None
            value = {"available": True, "run": run, "comparison": comparison,
                     "runs": sorted(runs, key=lambda r: r["created_at"], reverse=True)[:5]}
    except (urllib.error.URLError, OSError, ValueError, KeyError) as error:
        value = {"available": False, "error": f"Planner backend not reachable: {error}"}
    return value


def _recent_shares(creative: dict) -> dict:
    """Share of paid spend per channel over the creative overview's baseline + last week."""
    rows = (creative.get("channel_overview") or {}).get("channels") or []
    total = sum(r.get("spend_eur") or 0 for r in rows)
    return {r["channel"]: 100 * (r.get("spend_eur") or 0) / total for r in rows} if total else {}


def review(plan: dict, creative: dict, loi: dict, pacing: dict | None = None) -> dict:
    """The plan, per channel, with the agents' comments. Pure: no I/O."""
    paced = {c["channel"]: c for c in ((pacing or {}).get("channels") or [])}
    run = plan.get("run")
    if not plan.get("available"):
        return {"status": "OFFLINE", "detail": plan.get("error")}
    if not run:
        return {"status": "NO_PLAN", "detail": "Planner has no allocation yet."}
    config = run.get("config") or {}
    overview = {r["channel"]: r for r in ((creative.get("channel_overview") or {}).get("channels") or [])}
    shares_now = _recent_shares(creative)
    checks = (creative.get("watchdog") or {}).get("checks") or []
    orphans = next((c["items"] for c in checks if c["id"] == "spend_without_crm_leads"), [])
    gaps = next((c["items"] for c in checks if c["id"] == "tracking_gap"), [])
    fatigue = (creative.get("fatigue") or {}).get("top") or []
    quality = (creative.get("quality_vs_volume") or {}).get("channels") or {}
    loi_channels = {r["channel"]: r for r in ((loi.get("by_channel") or {}).get("channels") or [])}

    channels = []
    for row in sorted(run.get("channel") or [], key=lambda r: -(r.get("Budget") or 0)):
        name = row["Channel"]
        cname = CREATIVE_NAME.get(name, name)
        share = 100 * (row.get("Quota_Budget") or 0)
        recent = overview.get(cname) or {}
        comments = []
        if cname in shares_now and abs(share - shares_now[cname]) >= SHARE_SHIFT_PTS:
            verb = "raises" if share > shares_now[cname] else "lowers"
            comments.append({"tone": "info", "source": "creative data",
                             "text": f"The plan {verb} its share of budget to {share:.0f}% from {shares_now[cname]:.0f}% of actual paid spend last week."})
        change = recent.get("cpl_crm_change_pct")
        if change is not None and change >= CPL_RISE_PCT:
            comments.append({"tone": "warn", "source": "creative data",
                             "text": f"Cost per CRM lead rose {change:.0f}% last week vs the 4 before (EUR {recent.get('cpl_crm_eur')})."})
        elif change is not None and change <= CPL_FALL_PCT:
            comments.append({"tone": "info", "source": "creative data",
                             "text": f"Cost per CRM lead fell {abs(change):.0f}% last week vs the 4 before (EUR {recent.get('cpl_crm_eur')})."})
        model_cpl, actual_cpl = row.get("CPL"), recent.get("cpl_crm_eur")
        if model_cpl and actual_cpl and abs(model_cpl / actual_cpl - 1) * 100 >= MODEL_GAP_PCT:
            comments.append({"tone": "info", "source": "creative data",
                             "text": f"Planner expects EUR {model_cpl:.2f} per lead; last week's actual was EUR {actual_cpl:.2f} per CRM lead (whole channel, so comparable, not identical)."})
        own_orphans = [o for o in orphans if o.get("channel") == cname]
        if own_orphans:
            comments.append({"tone": "warn", "source": "data watchdog",
                             "text": f"{len(own_orphans)} creative(s) spend without reaching the CRM: their leads are invisible to the plan's inputs."})
        if any(g.get("channel") == cname for g in gaps):
            comments.append({"tone": "warn", "source": "data watchdog",
                             "text": "Platform and CRM leads drifted apart last week: a tracking break would distort this channel's CPL."})
        tired = [f for f in fatigue if f.get("channel") == cname]
        if tired:
            comments.append({"tone": "info", "source": "weekly briefing",
                             "text": f"{len(tired)} long-running creative(s) show falling CTR (fatigue)."})
        weak = (quality.get(cname) or {}).get("cheap_leads_weak_quality") or []
        if weak:
            comments.append({"tone": "info", "source": "weekly briefing",
                             "text": f"{len(weak)} creative(s) bring cheap leads that rarely reach an SC1."})
        pc = paced.get(cname)
        if pc and row.get("Budget"):
            ratio = pc["daily_budget_eur"] / row["Budget"]
            comments.append({"tone": "warn" if ratio >= 1.5 or ratio <= 0.67 else "info", "source": "budget pacing",
                             "text": f"Budgets set in the platform add up to EUR {pc['daily_budget_eur']:,.0f}/day against the plan's EUR {row['Budget']:,.0f}; last week it spent {pc['utilisation_pct']}% of the budgets set."})
        lc = loi_channels.get(cname)
        if lc and lc.get("signed_net_pct") is not None:
            comments.append({"tone": "info", "source": "LOI agent",
                             "text": f"Consultations from this channel: {lc['hot_pct']}% hot, {lc['signed_net_pct']}% signed (historical, calls to {(loi.get('by_channel') or {}).get('channel_tags_end')})."})
        channels.append({"channel": name, "budget_eur": round(row.get("Budget") or 0, 2), "share_pct": round(share, 1),
                         "leads": round(row.get("Lead") or 0, 1), "cpl_model_eur": round(model_cpl, 2) if model_cpl else None,
                         "recent_cpl_eur": actual_cpl, "recent_cpl_change_pct": change,
                         "recent_spend_share_pct": round(shares_now[cname], 1) if cname in shares_now else None,
                         "comments": comments})

    plan_comments = []
    source = config.get("data_source")
    if run.get("source_file"):
        plan_comments.append({"tone": "info", "source": "Planner",
                              "text": f"Production run #{run['id']} ({run.get('status')}), read from the app export {run['source_file']}. The local Planner holds only test runs."})
    if source != PRODUCTION_SOURCE:
        plan_comments.append({"tone": "warn", "source": "Planner",
                              "text": f"Built on the '{source}' data source: real volumes but placeholder economics. It shows the mechanics; it is not a plan to execute."})
    cal = loi.get("calibration") or {}
    if cal.get("planner_outside_ci"):
        plan_comments.append({"tone": "warn", "source": "LOI agent",
                              "text": f"EV inside this plan uses {len(cal['planner_outside_ci'])} LOI signing rate(s) that differ from what actually signed ({', '.join(cal['planner_outside_ci'])})."})
    comparison = plan.get("comparison")
    if comparison:
        recal = comparison.get("recalibration") or {}
        if recal.get("stato") == "errore":
            plan_comments.append({"tone": "warn", "source": "Planner",
                                  "text": f"The online-recalibration twin (#{comparison['b']['id']}) failed to recalibrate ({recal.get('motivo')}), so it is identical to this plan and the comparison says nothing."})
        else:
            plan_comments.append({"tone": "info", "source": "Planner",
                                  "text": f"Online recalibration moves EUR {comparison.get('budget_spostato', 0):.0f} ({comparison.get('budget_spostato_pct', 0):.1f}%) of budget vs this plan; lead change {comparison['delta'].get('lead_pct', 0):.1f}%."})
    if run.get("status") == "pending":
        plan_comments.append({"tone": "info", "source": "Planner",
                              "text": "Pending: approve or reject it in the Planner workspace. Nothing here changes it."})
    stale = (creative.get("watchdog") or {}).get("status") == "WARN"
    if stale:
        plan_comments.append({"tone": "info", "source": "data watchdog",
                              "text": "The creative data checks have open warnings; see the attention list before trusting per-channel comments."})

    summary = {row[0]: row[1] for row in (run.get("summary") or []) if isinstance(row, list) and len(row) >= 2}
    return {"status": "READY", "run_id": run["id"], "run_status": run.get("status"), "valid_for": run.get("valid_for"),
            "created_at": run.get("created_at"), "metodo": run.get("metodo"), "data_source": source,
            "budget_eur": summary.get("Budget speso"), "leads": summary.get("Lead attesi"), "cpl_eur": summary.get("CPL medio"),
            "active_cells": summary.get("Combo attive"), "economics_real": source == PRODUCTION_SOURCE,
            "channels": channels, "plan_comments": plan_comments,
            "other_runs": [{k: r.get(k) for k in ("id", "status", "valid_for", "metodo", "created_at")} for r in plan.get("runs", [])]}
