from __future__ import annotations

import csv
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from datetime import date, datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

# Settings come from the environment; a local .env (see .env.example) fills it in.
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env")
except ImportError:
    pass

import briefing_pdf  # noqa: E402
import budget_pacing  # noqa: E402
import creative_data  # noqa: E402
import creative_insights  # noqa: E402
import llm_agents  # noqa: E402
import loi_feed  # noqa: E402
import organic_leak_agent  # noqa: E402
import plan_review  # noqa: E402


APP_ROOT = Path(__file__).resolve().parent
WORKSPACE_ROOT = APP_ROOT.parent
# Sibling projects are located through environment variables (see .env.example).
PROJECT_ROOT = Path(os.getenv("ATTRIBUTION_PROJECT_ROOT", str(WORKSPACE_ROOT / "attribution-project")))
DIST_ROOT = APP_ROOT / "dist"
CONTROL_PATH = APP_ROOT / "control_state.json"
POLICY_PATH = PROJECT_ROOT / "config" / "monitoring_policy.json"
INCIDENT_ROOT = PROJECT_ROOT / "outputs" / "monitoring" / "incidents"
RUN_ROOT = PROJECT_ROOT / "outputs" / "monitoring" / "runs"
AUDIT_PATH = PROJECT_ROOT / "outputs" / "history_audit" / "summary.json"
EXTENDED_PATH = PROJECT_ROOT / "outputs" / "extended_history_2025_2026" / "summary.json"
PLANNER_ROOT = Path(os.getenv("PLANNER_ROOT", str(WORKSPACE_ROOT / "planner")))

RUN_LOCK = threading.Lock()
CREATIVE_LOCK = threading.Lock()
# The computed briefing sections (checks included), so /api/state never
# recomputes them: the UI polls, the computation takes seconds.
CREATIVE_SECTIONS_PATH = creative_data.CACHE_DIR / "briefing_sections.json"
LAST_RUN_PATH = APP_ROOT / "runtime" / "last_run.json"
# Days after which a source counts as stale on the freshness chips.
STALE_AFTER_DAYS = {"measurement": 7, "creative": 2, "loi": 3, "reasons": 8, "crm_export": 30, "budgets": 2}

DEFAULT_CONTROL = {
    "monitoring_enabled": False,
    "schedule_mode": "manual",
    "daily_time": "09:15",
    "notification_mode": "local_only",
    "last_scheduled_date": None,
    "last_manual_run_at_utc": None,
    "github_writes": False,
    "production_writes": False,
    # Opus 5.5 runs cost money; off until the operator turns it on.
    "llm_agents_enabled": False,
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def newest(pattern: str) -> Path | None:
    matches = [path for path in PROJECT_ROOT.glob(pattern) if path.is_file()]
    return max(matches, key=lambda path: path.stat().st_mtime, default=None)


def latest_measurement() -> Path | None:
    return newest("outputs/measurement_refresh_*/measurement_table_daily.csv")


def policy_for(measurement: Path | None) -> Path:
    """A snapshot may carry its own policy -- e.g. the allocator extract has no
    organic column, so the shared policy's required columns cannot apply."""
    local = measurement.parent / "monitoring_policy.json" if measurement else None
    return local if local and local.is_file() else POLICY_PATH


def local_port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.18):
            return True
    except OSError:
        return False


def live_git_state(root: Path) -> dict:
    """HEAD, branch and dirty files, read from the repo now -- local git only.

    The shadow summary records the tree as it was when the bridge was tested;
    reporting that as current is how the card came to say 'clean' over a tree
    with uncommitted edits.
    """
    def git(*args: str) -> str | None:
        try:
            completed = subprocess.run(["git", *args], cwd=root, capture_output=True,
                                       text=True, timeout=5, check=False)
        except (OSError, subprocess.SubprocessError):
            return None
        # Not stripped here: porcelain lines start with a meaningful space.
        return completed.stdout if completed.returncode == 0 else None

    commit = git("rev-parse", "HEAD")
    if commit is None:
        return {"available": False}
    dirty = [line[3:] for line in (git("status", "--porcelain") or "").splitlines() if line]
    return {"available": True, "commit": commit.strip(),
            "branch": (git("rev-parse", "--abbrev-ref", "HEAD") or "").strip(),
            "dirty_files": dirty}


def days_old(day: str | None) -> int | None:
    try:
        return (date.today() - date.fromisoformat(str(day)[:10])).days
    except ValueError:
        return None


def measurement_summary(path: Path | None) -> dict:
    if path is None:
        return {"available": False}
    first_date = None
    last_date = None
    rows = 0
    total_leads = 0.0
    organic_leads = 0.0
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for record in reader:
            day = record.get("date")
            if day:
                first_date = first_date or day
                last_date = day
            rows += 1
            try:
                total_leads += float(record.get("total_leads") or 0)
                organic_leads += float(record.get("dashboard_leads_organic") or 0)
            except ValueError:
                pass
    return {
        "available": True,
        "path": str(path),
        "start": first_date,
        "end": last_date,
        "rows": rows,
        "total_leads": round(total_leads),
        "organic_leads": round(organic_leads),
    }


def list_incidents() -> list[dict]:
    items: list[dict] = []
    for path in INCIDENT_ROOT.glob("*/incident.json"):
        payload = read_json(path, {})
        if not payload:
            continue
        payload["_path"] = str(path)
        items.append(payload)
    return sorted(items, key=lambda item: (item.get("status") == "RESOLVED", item.get("last_seen_at_utc", "")), reverse=False)


def list_runs() -> list[dict]:
    items = []
    for path in RUN_ROOT.glob("*/run_manifest.json"):
        payload = read_json(path, {})
        if payload:
            items.append(payload)
    return sorted(items, key=lambda item: item.get("generated_at_utc", ""), reverse=True)[:12]


def application_state() -> dict:
    control = {**DEFAULT_CONTROL, **(read_json(CONTROL_PATH, {}) or {})}
    measurement = measurement_summary(latest_measurement())
    sufficiency_path = newest("outputs/input_sufficiency_refresh_*/summary.json")
    sufficiency = read_json(sufficiency_path, {}) if sufficiency_path else {}
    classification_path = newest("outputs/observational_attribution_*/classification.json")
    classification = read_json(classification_path, {}) if classification_path else {}
    replay_path = newest("outputs/monitoring/replay_review_*/summary.json")
    replay = read_json(replay_path, {}) if replay_path else {}
    extended = read_json(EXTENDED_PATH, {}) or {}
    planner_path = newest("outputs/planner_shadow_*/summary.json")
    planner_shadow = read_json(planner_path, {}) if planner_path else {}
    repository = planner_shadow.get("planner", {})
    live_git = live_git_state(PLANNER_ROOT)
    planner = {
        "available": PLANNER_ROOT.exists(),
        "root": str(PLANNER_ROOT),
        "model": "v6 CatBoost ensemble",
        "entry_point": 'planner.pipeline.MODEL_REGISTRY["v6"]',
        "commit": live_git.get("commit", repository.get("commit")),
        "branch": live_git.get("branch", repository.get("branch")),
        "working_tree_clean": (not live_git["dirty_files"]) if live_git["available"]
                              else repository.get("working_tree_clean"),
        "dirty_files": live_git.get("dirty_files", []),
        # The commit the synthetic bridge test actually ran against.
        "bridge_tested_commit": repository.get("commit"),
        "remote_matches_expected": repository.get("remote_matches_expected"),
        "technical_bridge_status": planner_shadow.get("status", "NOT_TESTED"),
        "neutral_parity_passed": planner_shadow.get("neutral_parity", {}).get("passed"),
        "trained_models": planner_shadow.get("catboost_gate", {}).get("trained_models"),
        "shadow_allowed": bool(classification.get("planner_shadow_allowed")),
        "production_write_allowed": False,
        "production_write_performed": bool(planner_shadow.get("production_write_performed", False)),
        "evidence_class": planner_shadow.get("evidence_class"),
        "runtime": {
            "backend": local_port_open(8000),
            "frontend": local_port_open(5173),
            # localhost, NOT 127.0.0.1: the planner's backend allows exactly one CORS
            # origin, http://localhost:5173 (Planner backend/main.py). Serving the
            # embed from 127.0.0.1 makes every API call from it fail CORS -- the
            # page loads and nothing in it works.
            "frontend_url": "http://localhost:5173",
        },
    }
    incidents = list_incidents()
    # Aggregates only -- organic_leak_agent never returns lead ids.
    leak = organic_leak_agent.scan(WORKSPACE_ROOT)
    open_incidents = [item for item in incidents if item.get("status") not in {"RESOLVED", "DISMISSED"}]
    current_weekly = sufficiency.get("measurement_window", {}).get("weekly_observations")
    weekly = extended.get("usable_complete_weeks") or current_weekly
    attribution_result = classification.get("result_class", "NOT_RUN")

    # Every card says how old its input is. Agents run on whatever snapshot is on
    # disk; a stale snapshot is allowed, an unannounced one is not.
    data_age = days_old(measurement.get("end"))
    last_run = (list_runs() or [{}])[0]
    if last_run:
        watchdog_detail = (
            f"Last run {str(last_run.get('generated_at_utc', ''))[:10]}: "
            f"{last_run.get('watchdog_status', 'unknown')}. ")
    else:
        watchdog_detail = "Never run. "
    watchdog_detail += (f"Data through {measurement.get('end')} ({data_age} days old); "
                        "it never refreshes data by itself.")
    creative = read_json(CREATIVE_SECTIONS_PATH, {}) or {}
    creative_checks = creative.get("watchdog") or {}
    if creative_checks.get("checks"):
        warned = [c["title"] for c in creative_checks["checks"] if c["status"] == "warn"]
        watchdog_detail += (f" Creative data ({(creative.get('data') or {}).get('fetched_at_utc', '')[:10]}): "
                            + (f"{len(warned)} check(s) to look at." if warned else "all checks pass."))
    open_detail = "; ".join(
        f"{item.get('key')} on {item.get('event_date')} "
        f"({len(list(Path(item['_path']).parent.glob('investigation_*.json')))} evidence package(s))"
        for item in open_incidents)
    leak_age = days_old(leak.get("window", {}).get("last"))
    leak_detail = organic_leak_agent.summarise(leak)
    if leak_age is not None:
        leak_detail += f" CRM export ends {leak['window']['last']} ({leak_age} days old)."
    if live_git.get("available"):
        planner_detail = (f"Backend {'up' if planner['runtime']['backend'] else 'DOWN'} at "
                         f"{planner['commit'][:8]} on {planner['branch']}; "
                         + (f"{len(planner['dirty_files'])} uncommitted file(s): "
                            + ", ".join(planner["dirty_files"]) + ". "
                            if planner["dirty_files"] else "tree clean. "))
    else:
        planner_detail = "Planner repository not readable. "
    planner_detail += ("Shadow execution allowed." if planner["shadow_allowed"]
                      else "Scenario execution remains evidence-locked.")

    loi = loi_feed.cached() or {}
    pacing = budget_pacing.cached() or {}
    llm = llm_state(control, open_incidents)
    report = llm["analyst_report"] or {}
    if llm["running"] == "analyst":
        analyst_status = "RUNNING"
    elif not llm["key_present"]:
        analyst_status = "NO_KEY"
    elif not llm["enabled"]:
        analyst_status = "DISABLED"
    else:
        analyst_status = "REPORTED" if report.get("status") == "COMPLETED" else "READY"
    # A layout sample is stored like a report but was never written by the model:
    # showing its headline here made it read as a real briefing.
    real_report = report.get("status") == "COMPLETED" and not report.get("sample")
    analyst_detail = (
        f"{report['generated_at_utc'][:10]}: {report['output'].get('headline', '')} "
        f"(${report['cost_usd']:.2f})" if real_report
        else "No briefing written yet (the one stored is a layout sample). Writes the weekly briefing "
             "from the creative data: fatigue, quality vs volume, creative attributes, product mix.")
    if report.get("sample") and analyst_status == "REPORTED":
        analyst_status = "READY"

    context = llm["context"] or {}
    registry = read_json(PROJECT_ROOT / "config" / "event_registry.json", {}) or {}
    external = [e for e in registry.get("events", []) if str(e.get("event_type", "")).startswith("external_")]
    pending = [e for e in (context.get("output") or {}).get("events", []) if not e.get("review")]
    if llm["running"] == "context":
        context_status = "RUNNING"
    elif pending:
        context_status = "NEEDS_REVIEW"
    elif not llm["key_present"]:
        context_status = "NO_KEY"
    elif not llm["enabled"]:
        context_status = "DISABLED"
    else:
        context_status = "READY"
    context_detail = (f"{len(registry.get('events', []))} registered events, {len(external)} external. "
                      + (f"{len(pending)} candidate(s) from the last web scan await review."
                         if pending else "No candidates awaiting review."))

    agents = [
        {
            "id": "context",
            "name": "Context Scout",
            "role": "Searches the web for dated external events (incentives, energy prices, holidays) and proposes them for the event registry.",
            "status": context_status,
            "detail": context_detail,
        },
        {
            "id": "analyst",
            "name": "Weekly Briefing",
            "role": f"Writes the weekly briefing from the creative ad performance data, one call ({llm_agents.MODEL}).",
            "status": analyst_status,
            "detail": analyst_detail,
        },
        {
            "id": "watchdog",
            "name": "Data Watchdog",
            "role": "Checks freshness, gaps, reconciliation and unusual movements.",
            "status": "RUNNING" if RUN_LOCK.locked() else ("READY" if control["monitoring_enabled"] else "MANUAL"),
            "detail": watchdog_detail,
        },
        {
            "id": "forensics",
            "name": "Incident Investigator",
            "role": "Builds evidence packages for persistent anomalies.",
            "status": "NEEDS_REVIEW" if open_incidents else "READY",
            "detail": (f"{len(open_incidents)} local incident(s) awaiting review: {open_detail}."
                       if open_incidents else "No open incidents."),
        },
        {
            "id": "loi",
            "name": "LOI Agent",
            "role": "Reads the consultation scores: quality trends, meeting-flow health, and whether the planner's LOI signing rates still match reality.",
            "status": {"WARN": "NEEDS_REVIEW", "HEALTHY": "READY"}.get(loi.get("status"), "PAUSED"),
            "detail": (f"{loi.get('folder')}: " + "; ".join(c["title"] + (" — look" if c["status"] == "warn" else " — ok")
                                                          for c in loi.get("checks", []))
                       if loi else "Not run yet. Reads the newest LOI filled workbook."),
        },
        {
            "id": "organic_leak",
            "name": "Organic Leak Tracer",
            "role": "Finds leads labelled organic that arrived on a paid ad click.",
            "status": ({"TRACED": "NEEDS_REVIEW", "CLEAN": "READY"}
                       .get(leak.get("status"), "PAUSED")),
            "detail": leak_detail,
        },
        {
            "id": "attribution",
            "name": "Attribution Evidence Gate",
            "role": "Decides whether channel contribution is strong enough to translate into EV.",
            "status": attribution_result,
            # The classifier has only DATA_BLOCKED / UNRESOLVED (observational_attribution.py:66):
            # no amount of historical data opens it. Say what would.
            "detail": (classification.get("interpretation", "No classification available.")
                       + " Waiting by design, not broken: only an experiment can open this gate, and the first one "
                         "(a platform lift test) is on hold until the platform's lead conversion tracking is fixed."),
        },
        {
            "id": "ev",
            "name": "EV Translator",
            "role": "Turns an approved contribution range into incremental expected value.",
            "status": "LOCKED" if not classification.get("planner_shadow_allowed") else "READY",
            "detail": "No EV adjustment is released until the attribution evidence gate passes.",
        },
        {
            "id": "planner",
            "name": "Planner CatBoost",
            "role": "Runs approved EV scenarios through the existing v6 CatBoost allocator.",
            # CONNECTED means the backend answers, not merely that the folder exists.
            "status": ("CONNECTED" if planner["available"] and planner["runtime"]["backend"]
                       else "OFFLINE"),
            "detail": planner_detail,
        },
        {
            "id": "decision",
            "name": "Budget Decision",
            "role": "Presents a reviewed allocation comparison for a human decision.",
            "status": "LOCKED" if not classification.get("planner_shadow_allowed") else "REVIEW_REQUIRED",
            "detail": "The production CatBoost allocator is never changed from this app.",
        },
    ]
    return {
        "generated_at_utc": utc_now(),
        "control": control,
        "policy": read_json(policy_for(latest_measurement()), {}),
        "measurement": measurement,
        "sufficiency": sufficiency,
        "classification": classification,
        "replay": replay,
        "history_audit": read_json(AUDIT_PATH, {}),
        "extended_history": extended,
        "planner": planner,
        "incidents": incidents,
        "runs": list_runs(),
        "organic_leak": leak,
        "agents": agents,
        "llm": llm,
        "creative": creative,
        "loi": loi,
        "freshness": freshness(measurement, creative, loi, leak, pacing),
        "allocation": plan_review.review(plan_review.latest_plan(), creative, loi, pacing),
        "pacing": pacing,
        "last_run": read_json(LAST_RUN_PATH, {}) or {},
        "summary": {
            "weekly_observations": weekly,
            "current_weekly_observations": extended.get("current_weeks") or current_weekly,
            "open_incidents": len(open_incidents),
            "attribution_result": attribution_result,
            "production_locked": True,
        },
    }


def freshness(measurement: dict, creative: dict, loi: dict, leak: dict, pacing: dict | None = None) -> list[dict]:
    """How old each source is. Agents run on what is on disk; the header says how old that is."""
    reasons = loi.get("reasons") or {}
    sources = [
        ("measurement", "Measurement table", measurement.get("end")),
        ("creative", "Creative sheet", ((creative.get("data") or {}).get("tabs") or {}).get("spend", {}).get("max_date")),
        ("loi", "LOI scores", loi.get("latest_scored_call")),
        ("reasons", "Call reasons", (reasons.get("generated_at_utc") or "")[:10] or None),
        ("crm_export", "CRM export", (leak.get("window") or {}).get("last")),
        ("budgets", "Platform budgets", (pacing or {}).get("latest_snapshot")),
    ]
    out = []
    for key, label, day in sources:
        age = days_old(day)
        out.append({"id": key, "label": label, "date": day, "age_days": age,
                    "stale": age is None or age > STALE_AFTER_DAYS[key]})
    return out


def llm_state(control: dict, open_incidents: list[dict]) -> dict:
    """What the Opus agents can do right now and what they last produced."""
    return {
        "model": llm_agents.MODEL,
        "enabled": bool(control.get("llm_agents_enabled")),
        "key_present": llm_agents.api_key() is not None,
        "running": llm_agents.STATUS["running"],
        "last_error": llm_agents.STATUS["last_error"],
        "max_cost_usd": llm_agents.MAX_COST_USD,
        "monthly_budget_usd": llm_agents.MONTHLY_BUDGET_USD,
        "spent_this_month_usd": llm_agents.spent_this_month(),
        "context": llm_agents.latest_context(),
        "analyst_report": llm_agents.latest_analyst_report(),
        "hypotheses": {item["incident_id"]: llm_agents.latest_hypothesis(item["incident_id"])
                       for item in open_incidents if item.get("incident_id")},
    }


def review_hypothesis(incident_id: str, payload: dict) -> dict:
    """Accept, relabel or dismiss an Opus suggestion. The human's click is the decision."""
    record = llm_agents.latest_hypothesis(incident_id)
    if not record or record.get("status") != "COMPLETED":
        raise FileNotFoundError("No completed hypothesis for this incident")
    if record.get("sample"):
        raise ValueError("This is a layout sample, not an Opus result; it cannot change the incident.")
    action = payload.get("action")
    suggested = record["output"]["suggested_label"]
    if action == "dismiss":
        return {"status": "SAVED", "hypothesis": llm_agents.review_hypothesis(incident_id, "dismiss")}
    if action == "accept":
        label = suggested
    elif action == "relabel":
        label = payload.get("label")
        if label not in llm_agents.INCIDENT_LABELS:
            raise ValueError(f"label must be one of {', '.join(llm_agents.INCIDENT_LABELS)}")
    else:
        raise ValueError("action must be accept, relabel or dismiss")
    # UNRESOLVED is a label, not a closure: the incident stays under investigation.
    status = "INVESTIGATING" if label == "UNRESOLVED" else "RESOLVED"
    note = (f"Accepted {llm_agents.MODEL} suggestion" if action == "accept"
            else f"Relabelled; {llm_agents.MODEL} had suggested {suggested}")
    incident = update_incident(incident_id, {"status": status, "label": label,
                                             "note": f"{note}. {record['output']['hypothesis']}"})
    llm_agents.review_hypothesis(incident_id, action, label)
    return {"status": "SAVED", "incident": incident}


def start_llm_agent(payload: dict) -> dict:
    control = {**DEFAULT_CONTROL, **(read_json(CONTROL_PATH, {}) or {})}
    if not control.get("llm_agents_enabled"):
        raise ValueError("LLM agents are switched off (llm_agents_enabled).")
    if llm_agents.api_key() is None:
        raise ValueError("No ANTHROPIC_API_KEY in the environment or .env.")
    agent = payload.get("agent")
    if agent not in {"analyst", "investigator", "context"}:
        raise ValueError("agent must be analyst, investigator or context")
    incident_id = payload.get("incident_id") if agent == "investigator" else None
    llm_agents.run_in_background(agent, incident_id)
    return {"status": "STARTED", "agent": agent, "incident_id": incident_id}


def safe_policy_update(payload: dict) -> dict:
    # Edit the policy the runs actually use, not always the shared one.
    path = policy_for(latest_measurement())
    current = read_json(path, {}) or {}
    bounds = {
        "max_staleness_days": (0, 30, int),
        "same_weekday_history_points": (4, 26, int),
        "robust_z_threshold": (2.0, 10.0, float),
        "persistence_window_days": (1, 14, int),
        "persistence_min_flags": (1, 14, int),
        "incident_cooldown_days": (0, 30, int),
    }
    for key, (lower, upper, value_type) in bounds.items():
        if key not in payload:
            continue
        value = value_type(payload[key])
        if value < lower or value > upper:
            raise ValueError(f"{key} must be between {lower} and {upper}")
        current[key] = value
    if current.get("persistence_min_flags", 1) > current.get("persistence_window_days", 1):
        raise ValueError("persistence_min_flags cannot exceed persistence_window_days")
    atomic_json(path, current)
    return current


def safe_control_update(payload: dict) -> dict:
    current = {**DEFAULT_CONTROL, **(read_json(CONTROL_PATH, {}) or {})}
    if "monitoring_enabled" in payload:
        current["monitoring_enabled"] = bool(payload["monitoring_enabled"])
    if "schedule_mode" in payload:
        if payload["schedule_mode"] not in {"manual", "daily"}:
            raise ValueError("schedule_mode must be manual or daily")
        current["schedule_mode"] = payload["schedule_mode"]
    if "daily_time" in payload:
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", str(payload["daily_time"])):
            raise ValueError("daily_time must use HH:MM")
        current["daily_time"] = payload["daily_time"]
    if "llm_agents_enabled" in payload:
        current["llm_agents_enabled"] = bool(payload["llm_agents_enabled"])
    current["notification_mode"] = "local_only"
    current["github_writes"] = False
    current["production_writes"] = False
    atomic_json(CONTROL_PATH, current)
    return current


def locate_media_gviz(measurement: Path) -> Path | None:
    raw = measurement.parent / "raw_gviz"
    if not raw.exists():
        return None
    matches = sorted(raw.glob("*main_metrics*.txt"))
    return matches[0] if matches else None


def run_monitoring(trigger: str = "manual") -> dict:
    if not RUN_LOCK.acquire(blocking=False):
        raise RuntimeError("A monitoring cycle is already running")
    try:
        measurement = latest_measurement()
        if measurement is None:
            raise RuntimeError("No completed measurement table is available")
        run_id = datetime.now().strftime("%Y-%m-%d_%H%M%S") + f"_{trigger}"
        analysis_as_of = measurement_summary(measurement).get("end") or date.today().isoformat()
        python_exe = PROJECT_ROOT / ".venv-shadow" / "Scripts" / "python.exe"
        executable = str(python_exe if python_exe.exists() else Path(sys.executable))
        command = [
            executable,
            "-m",
            "organic_attribution.cli",
            "run-monitoring-cycle",
            "--measurement-csv",
            str(measurement),
            "--policy-json",
            str(policy_for(measurement)),
            "--output-root",
            str(RUN_ROOT),
            "--incident-root",
            str(INCIDENT_ROOT),
            "--run-id",
            run_id,
            "--analysis-as-of",
            str(analysis_as_of),
        ]
        media = locate_media_gviz(measurement)
        if media:
            command.extend(["--media-gviz", str(media)])
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(PROJECT_ROOT / "src")
        completed = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        # The CLI exits non-zero on DATA_QUALITY_FAILED / BLOCKED_STALE too. That
        # is a finding, not a crash: if the run wrote its manifest, it completed.
        manifest = read_json(RUN_ROOT / run_id / "run_manifest.json")
        if completed.returncode != 0 and not manifest:
            raise RuntimeError((completed.stderr or completed.stdout or "Monitoring cycle failed")[-2000:])
        control = {**DEFAULT_CONTROL, **(read_json(CONTROL_PATH, {}) or {})}
        if trigger == "manual":
            control["last_manual_run_at_utc"] = utc_now()
        else:
            control["last_scheduled_date"] = date.today().isoformat()
        atomic_json(CONTROL_PATH, control)
        # The creative checks ride along on the cached sheet: a monitoring run
        # never fetches, the same rule as the measurement table.
        loi_status = None
        try:
            loi_status = loi_snapshot()["loi_status"]
        except Exception as error:                          # noqa: BLE001
            loi_status = f"FAILED: {error}"
        creative_status = None
        pacing_status = None
        if creative_data.meta() is not None or trigger == "manual":
            try:
                # A manual run is the "show me the latest" button: it fetches the
                # creative sheet (read-only, ~30s). A scheduled run reuses the cache.
                creative_status = creative_snapshot(fetch=trigger == "manual")["watchdog_status"]
            except Exception as error:                      # noqa: BLE001
                creative_status = f"FAILED: {error}"
        # Budgets as set in Meta/Google (read-only API calls), after the creative
        # sheet so spend carries campaign ids. Manual runs only, like the sheet.
        if trigger == "manual":
            try:
                pacing_status = budget_pacing.refresh()["status"]
            except Exception as error:                      # noqa: BLE001
                pacing_status = f"FAILED: {error}"
        summary = {
            "status": "COMPLETED",
            "finished_at_utc": utc_now(),
            "trigger": trigger,
            "creative_status": creative_status,
            "loi_status": loi_status,
            "pacing_status": pacing_status,
            "run_status": (manifest or {}).get("status"),
            "run_id": run_id,
            "analysis_as_of": analysis_as_of,
        }
        atomic_json(LAST_RUN_PATH, summary)
        return {**summary, "stdout": completed.stdout[-2000:]}
    finally:
        RUN_LOCK.release()


def creative_snapshot(fetch: bool) -> dict:
    """Recompute the creative sections, fetching the sheet first when asked. Manual only."""
    if not CREATIVE_LOCK.acquire(blocking=False):
        raise RuntimeError("Creative data is already being refreshed")
    try:
        if fetch:
            creative_data.refresh()
        if creative_data.meta() is None:
            raise FileNotFoundError("No creative data yet: refresh it first.")
        sections = creative_insights.briefing_sections()
        CREATIVE_SECTIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CREATIVE_SECTIONS_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(sections, default=str), encoding="utf-8")
        os.replace(tmp, CREATIVE_SECTIONS_PATH)
        return {"status": "COMPLETED", "fetched_at_utc": sections["data"]["fetched_at_utc"],
                "watchdog_status": sections["watchdog"]["status"]}
    finally:
        CREATIVE_LOCK.release()


def loi_snapshot() -> dict:
    """Recompute the LOI feed from the newest filled workbook. Local file, aggregates only."""
    if not CREATIVE_LOCK.acquire(blocking=False):
        raise RuntimeError("A creative or LOI refresh is already running")
    try:
        feed = loi_feed.refresh()
        return {"status": "COMPLETED", "loi_status": feed["status"], "file": feed["file"]}
    finally:
        CREATIVE_LOCK.release()


def update_incident(incident_id: str, payload: dict) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", incident_id):
        raise ValueError("Invalid incident id")
    path = INCIDENT_ROOT / incident_id / "incident.json"
    if not path.exists():
        raise FileNotFoundError("Incident not found")
    incident = read_json(path, {}) or {}
    status = payload.get("status")
    allowed = {"OPEN", "INVESTIGATING", "RESOLVED", "DISMISSED"}
    if status not in allowed:
        raise ValueError("Invalid incident status")
    incident["status"] = status
    incident["owner_review"] = {
        "updated_at_utc": utc_now(),
        "label": str(payload.get("label") or "UNRESOLVED")[:80],
        "note": str(payload.get("note") or "")[:1000],
    }
    atomic_json(path, incident)
    return incident


def scheduler_loop() -> None:
    while True:
        try:
            control = {**DEFAULT_CONTROL, **(read_json(CONTROL_PATH, {}) or {})}
            local_now = datetime.now()
            due = (
                control["monitoring_enabled"]
                and control["schedule_mode"] == "daily"
                and local_now.strftime("%H:%M") >= control["daily_time"]
                and control.get("last_scheduled_date") != local_now.date().isoformat()
            )
            if due:
                run_monitoring("scheduled")
        except Exception:
            pass
        time.sleep(30)


class Handler(BaseHTTPRequestHandler):
    server_version = "AttributionControl/1.0"

    def log_message(self, fmt: str, *args) -> None:
        print(f"[{self.log_date_time_string()}] {fmt % args}")

    def send_json(self, payload: object, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 1_000_000:
            raise ValueError("Request too large")
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw.decode("utf-8"))

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/state":
            self.send_json(application_state())
            return
        if path == "/api/briefing/pdf":
            # Built on request from the current state, so it always matches the Overview.
            try:
                pdf = briefing_pdf.build(application_state())
            except Exception as exc:                            # noqa: BLE001
                self.send_json({"error": f"Could not build the PDF: {exc}"}, 500)
                return
            content = pdf.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Disposition", f'attachment; filename="{pdf.name}"')
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(content)
            return
        requested = "index.html" if path in {"", "/"} else path.lstrip("/")
        target = (DIST_ROOT / requested).resolve()
        if DIST_ROOT.resolve() not in target.parents and target != DIST_ROOT.resolve():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not target.exists() or not target.is_file():
            target = DIST_ROOT / "index.html"
        content = target.read_bytes()
        mime = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".json": "application/json; charset=utf-8",
        }.get(target.suffix, "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            body = self.read_body()
            if path == "/api/run":
                self.send_json(run_monitoring("manual"))
                return
            if path == "/api/control":
                self.send_json({"status": "SAVED", "control": safe_control_update(body)})
                return
            if path == "/api/creative/refresh":
                self.send_json(creative_snapshot(fetch=bool(body.get("fetch", True))))
                return
            if path == "/api/loi/refresh":
                self.send_json(loi_snapshot())
                return
            if path == "/api/llm/run":
                self.send_json(start_llm_agent(body))
                return
            if path == "/api/policy":
                self.send_json({"status": "SAVED", "policy": safe_policy_update(body)})
                return
            if path == "/api/context/review":
                self.send_json({"status": "SAVED", "event": llm_agents.review_context_event(
                    str(body.get("run", "")), int(body.get("index", -1)), str(body.get("action", "")))})
                return
            match = re.fullmatch(r"/api/incidents/([^/]+)/hypothesis", path)
            if match:
                if not re.fullmatch(r"[A-Za-z0-9_.-]+", match.group(1)):
                    raise ValueError("Invalid incident id")
                self.send_json(review_hypothesis(match.group(1), body))
                return
            match = re.fullmatch(r"/api/incidents/([^/]+)/status", path)
            if match:
                self.send_json({"status": "SAVED", "incident": update_incident(match.group(1), body)})
                return
            self.send_json({"error": "Not found"}, 404)
        except FileNotFoundError as exc:
            self.send_json({"error": str(exc)}, 404)
        except (ValueError, json.JSONDecodeError) as exc:
            self.send_json({"error": str(exc)}, 400)
        except Exception as exc:
            self.send_json({"error": str(exc)}, 500)


def main() -> int:
    if not CONTROL_PATH.exists():
        atomic_json(CONTROL_PATH, DEFAULT_CONTROL)
    threading.Thread(target=scheduler_loop, daemon=True).start()
    threading.Thread(target=organic_leak_agent.scan,
                     args=(WORKSPACE_ROOT,), daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", 8765), Handler)
    print("Planner Attribution Network: http://127.0.0.1:8765")
    print("Local-only. Production writes, GitHub writes and external notifications are disabled.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
