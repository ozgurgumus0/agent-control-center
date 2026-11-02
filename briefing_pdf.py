"""The weekly briefing as a PDF to share with the team and management.

Built from the control center's state (the same numbers as the Overview):
the written briefing first when one exists, then the Planner plan with the
agents' comments, what needs attention, consultation quality and the call
digest; the supporting tables go to the appendix. Narrative first, one key
number per point, statistics at the back.

Aggregates only, like everything the agents show. Nothing here recommends a
budget: the plan stays a decision made in Planner.
"""
from __future__ import annotations

import os
import re
from datetime import date, datetime
from pathlib import Path

from reportlab.lib.colors import HexColor
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

APP_ROOT = Path(__file__).resolve().parent
EXPORT_DIR = APP_ROOT / "runtime" / "exports"
# Optional logo and footer text, set through the environment (see .env.example).
LOGO = Path(os.getenv("BRIEFING_LOGO_PATH", str(APP_ROOT / "assets" / "logo.png")))
AUTHOR = os.getenv("BRIEFING_AUTHOR", "Marketing Analytics")
ORG_NAME = os.getenv("BRIEFING_ORG_NAME", "Marketing intelligence")

# The status-report palette.
NAVY, NAVY_2, BLUE = HexColor("#0A2540"), HexColor("#1F4675"), HexColor("#1A6DD4")
GREEN, ORANGE, TEXT = HexColor("#2E9E5B"), HexColor("#D9820B"), HexColor("#27364A")
MUTED, RULE, PALE, PALE_BLUE, WHITE = (HexColor("#5B6475"), HexColor("#C9D1DA"), HexColor("#F5F6F8"),
                                       HexColor("#EAF1F8"), HexColor("#FFFFFF"))

BODY = ParagraphStyle("Body", fontName="Helvetica", fontSize=10, leading=14.5, textColor=TEXT, alignment=TA_LEFT)
SMALL = ParagraphStyle("Small", parent=BODY, fontSize=8.5, leading=12, textColor=MUTED)
LEAD = ParagraphStyle("Lead", parent=BODY, fontSize=13, leading=18, textColor=NAVY)
H1 = ParagraphStyle("H1", parent=BODY, fontName="Helvetica-Bold", fontSize=15, leading=19, textColor=NAVY, spaceBefore=6, spaceAfter=6)
H2 = ParagraphStyle("H2", parent=BODY, fontName="Helvetica-Bold", fontSize=11.5, leading=15, textColor=NAVY_2, spaceBefore=8, spaceAfter=3)
KICKER = ParagraphStyle("Kicker", parent=BODY, fontName="Helvetica-Bold", fontSize=8, leading=10, textColor=BLUE)
CELL = ParagraphStyle("Cell", parent=BODY, fontSize=8.3, leading=10.8)
CELL_HEAD = ParagraphStyle("CellHead", parent=CELL, fontName="Helvetica-Bold", textColor=WHITE)

LABELS = {"CONFIRMED_OPERATIONAL_CHANGE": "Operational change", "EXPECTED_VARIATION": "Expected variation",
          "DATA_QUALITY_ISSUE": "Data-quality issue", "UNRESOLVED": "Unresolved"}
FOLLOW = {"held": "Held", "did_not_hold": "Did not hold", "cannot_tell": "Can't tell yet"}
# A warning names the problem; check titles state the healthy case (same map as the web page).
PROBLEM = {"freshness": "Creative data is out of date", "tracking_gap": "Platform and CRM leads drifted apart",
           "spend_without_crm_leads": "Creatives spending without reaching the CRM",
           "non_marketing_spend": "Non-marketing spend inside main_metrics",
           "untagged_creatives": "Spending creatives missing from Ad Details"}


def clean(text) -> str:
    """Plain text for the built-in PDF fonts, escaped for reportlab's mini-markup."""
    s = str("" if text is None else text)
    for a, b in (("→", "to"), ("−", "-"), ("—", "-"), ("–", "-"), ("≥", ">="), ("≤", "<="), ("…", "..."), ("’", "'"), ("“", '"'), ("”", '"')):
        s = s.replace(a, b)
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def bold_numbers(text: str) -> str:
    """Bold real figures (percentages, decimals, thousands, amounts), not day numbers, years or small counts."""
    def pick(m):
        lead, num = m.group(1), m.group(2)
        core = num.rstrip(".,")
        is_year = re.fullmatch(r"20\d\d", core) is not None
        figure = "%" in core or "." in core.strip("+-") or "," in core or (len(core.strip("+-")) >= 3 and not is_year)
        return f"{lead}<b>{core}</b>{num[len(core):]}" if figure else m.group(0)
    return re.sub(r"(^|[\s(€$])([+-]?\d[\d.,]*%?)(?=[\s).,;:]|$)", pick, text)


def para(text, style=BODY, numbers=True) -> Paragraph:
    body = clean(text)
    return Paragraph(bold_numbers(body) if numbers else body, style)


def eur(v) -> str:
    return "-" if v is None else f"EUR {v:,.0f}" if abs(v) >= 100 else f"EUR {v:,.2f}"


def pct(v, d=1) -> str:
    return "-" if v is None else f"{v:.{d}f}%"


def delta(v) -> str:
    return "-" if v is None else f"{'+' if v > 0 else ''}{v:.1f}%"


def day(text) -> str:
    try:
        return datetime.fromisoformat(str(text)[:10]).strftime("%d %b %Y")
    except ValueError:
        return str(text or "")


def table(rows: list[list], widths: list[float], head=True) -> Table:
    data = [[Paragraph(clean(c) if isinstance(c, str) else str(c), CELL_HEAD if head and i == 0 else CELL) for c in row]
            for i, row in enumerate(rows)]
    t = Table(data, colWidths=widths, repeatRows=1 if head else 0)
    style = [("VALIGN", (0, 0), (-1, -1), "TOP"), ("LINEBELOW", (0, 0), (-1, -1), 0.4, RULE),
             ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
             ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5)]
    if head:
        style += [("BACKGROUND", (0, 0), (-1, 0), NAVY_2)]
    style += [("BACKGROUND", (0, r), (-1, r), PALE) for r in range(2 if head else 1, len(rows), 2)]
    t.setStyle(TableStyle(style))
    return t


def callout(flowables: list, color=BLUE) -> Table:
    t = Table([[flowables]], colWidths=[170 * mm])
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), PALE_BLUE), ("LINEBEFORE", (0, 0), (0, -1), 3, color),
                           ("LEFTPADDING", (0, 0), (-1, -1), 10), ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                           ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8)]))
    return t


# ----------------------------------------------------------------- content

def attention(state: dict) -> list[tuple[str, str]]:
    """What a person should look at, as (headline, detail). Mirrors the Overview's list."""
    items = []
    for inc in state.get("incidents", []):
        if inc.get("status") in {"RESOLVED", "DISMISSED"}:
            continue
        hyp = (state.get("llm", {}).get("hypotheses") or {}).get(inc.get("incident_id")) or {}
        sub = (f"The Investigator suggests {LABELS.get(hyp['output']['suggested_label'], '').lower()} "
               f"({hyp['output']['confidence']} confidence)." if hyp.get("status") == "COMPLETED" and not hyp.get("sample")
               else "No Investigator hypothesis yet.")
        items.append((f"Incident to label: {inc.get('key')} on {day(inc.get('event_date'))}", sub))
    cal = (state.get("loi") or {}).get("calibration") or {}
    if cal.get("planner_outside_ci"):
        items.append((f"{len(cal['planner_outside_ci'])} LOI signing rates in Planner differ from what actually signed",
                      f"{', '.join(cal['planner_outside_ci'])}. Planner uses these inside EV: a decision for the Planner owner."))
    for check in ((state.get("creative") or {}).get("watchdog") or {}).get("checks", []):
        if check.get("status") == "warn":
            items.append((PROBLEM.get(check.get("id"), check["title"]), check["detail"]))
    for f in state.get("freshness", []):
        if f.get("stale"):
            items.append((f"{f['label']} is {f.get('age_days') if f.get('age_days') is not None else '?'} days old",
                          "Results that depend on it are as old as it is."))
    return items


def briefing_section(state: dict) -> list:
    out = [para("This week in one read", H1, numbers=False)]
    report = (state.get("llm") or {}).get("analyst_report") or {}
    if report.get("status") == "COMPLETED" and not report.get("sample"):
        b = report["output"]
        out += [para(b.get("headline"), LEAD), Spacer(1, 6)]
        for i, f in enumerate(b.get("findings", []), 1):
            out.append(KeepTogether([para(f"{i}. {f['title']}  -  {f['key_number']}", H2), para(f["story"])]))
        if b.get("follow_up"):
            out += [para("Last week's watch items", H2, numbers=False),
                    table([["Item", "Outcome", "What the data shows"]] +
                          [[x["item"], FOLLOW.get(x["verdict"], x["verdict"]), x["note"]] for x in b["follow_up"]],
                          [55 * mm, 25 * mm, 90 * mm])]
        if b.get("watch_items"):
            out += [para("Check next week", H2, numbers=False)] + [para(f"{i}. {w}") for i, w in enumerate(b["watch_items"], 1)]
        out.append(para(f"Written by {report.get('model')} from the computed sections below.", SMALL, numbers=False))
    else:
        out.append(callout([para("The written briefing has not been generated this week (the LLM agents are switched off). "
                                 "Everything below is computed directly from the data by the agents.", BODY, numbers=False)], ORANGE))
    return out


def plan_section(state: dict) -> list:
    a = state.get("allocation") or {}
    out = [para("The allocation plan", H1, numbers=False)]
    if a.get("status") != "READY":
        return out + [para(a.get("detail") or "No Planner plan available.")]
    out.append(para(f"Planner plan #{a['run_id']} for {day(a['valid_for'])} ({a.get('metodo') or 'classic'}), "
                    f"status {a.get('run_status')}: budget {eur(a.get('budget_eur'))}, {a.get('leads') or 0:.0f} expected leads, "
                    f"average cost per lead {eur(a.get('cpl_eur'))}.", LEAD))
    if not a.get("economics_real"):
        out.append(callout([para(f"Built on the '{a.get('data_source')}' data source: real volumes, placeholder economics. "
                                 "It shows the mechanics; it is not a plan to execute.", BODY, numbers=False)], ORANGE))
    out += [Spacer(1, 4), para("What the agents say about the plan", H2, numbers=False)]
    # The test-data warning is already the callout above; do not say it twice.
    out += [para(f"- {c['text']}") for c in a.get("plan_comments", [])
            if a.get("economics_real") or "placeholder economics" not in c["text"]]
    out.append(para("Per channel", H2, numbers=False))
    rows = [["Channel", "Budget", "Share", "Spend share last week", "Planner CPL", "Actual CPL last week"]]
    for c in a.get("channels", []):
        rows.append([c["channel"], eur(c["budget_eur"]), pct(c["share_pct"], 0), pct(c.get("recent_spend_share_pct"), 0),
                     eur(c.get("cpl_model_eur")), f"{eur(c.get('recent_cpl_eur'))} ({delta(c.get('recent_cpl_change_pct'))})"
                     if c.get("recent_cpl_eur") else "-"])
    out.append(table(rows, [27 * mm, 25 * mm, 18 * mm, 36 * mm, 26 * mm, 38 * mm]))
    for c in a.get("channels", []):
        if c.get("comments"):
            out.append(KeepTogether([para(c["channel"], H2, numbers=False)] + [para(f"- {x['text']}") for x in c["comments"]]))
    pacing = state.get("pacing") or {}
    if pacing.get("channels"):
        out.append(para("Budgets set in the platforms vs what was spent", H2, numbers=False))
        out.append(table([["Channel", "Budgets set / day", "Spent last week", "Share of budget spent", "Not spending", "Budget-limited"]] +
                         [[c["channel"], eur(c["daily_budget_eur"]), eur(c["week_spend_eur"]), pct(c["utilisation_pct"]),
                           str(c["under_delivering"]), str(c["budget_limited"])] for c in pacing["channels"]],
                         [30 * mm, 30 * mm, 28 * mm, 34 * mm, 24 * mm, 24 * mm]))
        out.append(para(pacing.get("note", ""), SMALL, numbers=False))
    out.append(para("Comments describe; the budget is a human decision made in Planner.", SMALL, numbers=False))
    return out


def attention_section(state: dict) -> list:
    items = attention(state)
    out = [para("What needs attention", H1, numbers=False)]
    if not items:
        return out + [para("Nothing needs a look this week.")]
    for headline, detail in items:
        out.append(KeepTogether([para(headline, H2), para(detail)]))
    return out


def consultation_section(state: dict) -> list:
    loi = state.get("loi") or {}
    out = [para("What the consultations say", H1, numbers=False)]
    weekly = (loi.get("weekly_quality") or {}).get("last_vs_prior_4w") or {}
    names = {"mean_readiness": "Mean closing readiness", "hot_pct": "Hot share (readiness 70+)", "negative_pct": "Non idoneo / non interessato",
             "cancelled_pct": "Meetings cancelled", "no_recording_pct": "Held meetings without a recording"}
    if weekly:
        out.append(para(f"Week of {day((loi.get('weekly_quality') or {}).get('through_week'))} against the four weeks before, all channels:"))
        out.append(table([["Measure", "This week", "Prior 4 weeks", "Change (points)"]] +
                         [[names.get(k, k), f"{v['now']}", f"{v['prior_4w']}", f"{'+' if v['points'] > 0 else ''}{v['points']}"]
                          for k, v in weekly.items()], [70 * mm, 30 * mm, 35 * mm, 35 * mm]))
    digest = (loi.get("reasons") or {}).get("digest") or {}
    if digest.get("movers"):
        out.append(para("Last week in the calls", H2, numbers=False))
        out += [para(f"- {m['name']} ({m['group'].replace('_', ' ')}): {m['share_pct']}% of consultations vs {m['prior_4w_pct']}% before.")
                for m in digest["movers"]]
        out.append(para(digest.get("note", ""), SMALL, numbers=False))
    cal = loi.get("calibration") or {}
    if cal:
        out.append(para("Do LOI bands sign at the rates Planner assumes?", H2, numbers=False))
        rows = [["Band", "Mature calls", "Signed", "95% range", "Planner assumes", "Match"]]
        for r in cal.get("ai_bands", []) + cal.get("seller_fallback", []):
            ci = r.get("ci95_pct")
            rows.append([r["band"], str(r["calls"]), pct(r.get("rate_pct")), f"{ci[0]}-{ci[1]}%" if ci else "-",
                         pct(r.get("planner_pct")), "-" if r.get("planner_inside_ci") is None else ("yes" if r["planner_inside_ci"] else "NO")])
        out.append(table(rows, [52 * mm, 24 * mm, 20 * mm, 28 * mm, 28 * mm, 18 * mm]))
        out.append(para(cal.get("note", ""), SMALL, numbers=False))
    return out


def appendix(state: dict) -> list:
    creative = state.get("creative") or {}
    out = [Spacer(1, 14), para("Appendix: supporting numbers", H1, numbers=False)]
    overview = creative.get("channel_overview") or {}
    if overview.get("channels"):
        out.append(para(f"Channels, week of {day(overview.get('week'))} vs the 4 before", H2, numbers=False))
        out.append(table([["Channel", "Spend", "Change", "CRM leads", "Change", "Cost per CRM lead", "Change"]] +
                         [[r["channel"], eur(r["spend_eur"]), delta(r["spend_change_pct"]), f"{r['crm_leads']:.0f}",
                           delta(r["crm_leads_change_pct"]), eur(r["cpl_crm_eur"]), delta(r["cpl_crm_change_pct"])]
                          for r in overview["channels"]], [30 * mm, 24 * mm, 18 * mm, 22 * mm, 18 * mm, 34 * mm, 18 * mm]))
    fatigue = creative.get("fatigue") or {}
    out.append(para("Creative fatigue", H2, numbers=False))
    out.append(para(f"{fatigue.get('flagged', 0)} of {fatigue.get('creatives_checked', 0)} long-running creatives flagged "
                    f"({fatigue.get('rule', '')})."))
    out.append(para("Data sources and freshness", H2, numbers=False))
    out.append(table([["Source", "Data through", "Age (days)", "Stale"]] +
                     [[f["label"], f.get("date") or "-", str(f.get("age_days") if f.get("age_days") is not None else "-"),
                       "yes" if f.get("stale") else "no"] for f in state.get("freshness", [])],
                     [60 * mm, 40 * mm, 30 * mm, 30 * mm]))
    out.append(Spacer(1, 8))
    out.append(para("How to read this report: every figure is an aggregate computed locally from the creative ad performance sheet, "
                    "the LOI MVP workbook and the latest Planner plan. Platform leads and CRM leads are different counts. "
                    "SC1 figures for the last two weeks and contracts for the last eight are still arriving. Nothing here proves "
                    "that a channel causes leads, and nothing here is a budget recommendation.", SMALL, numbers=False))
    return out


# ------------------------------------------------------------------- build

def _page(canvas, doc, title: str, subtitle: str) -> None:
    canvas.saveState()
    w, h = A4
    canvas.setFillColor(NAVY)
    canvas.rect(0, h - 26 * mm, w, 26 * mm, stroke=0, fill=1)
    if LOGO.is_file():
        canvas.drawImage(str(LOGO), 18 * mm, h - 18 * mm, width=24 * mm, height=9 * mm, mask="auto", preserveAspectRatio=True)
    canvas.setFillColor(WHITE)
    canvas.setFont("Helvetica-Bold", 13)
    canvas.drawRightString(w - 18 * mm, h - 12.5 * mm, title)
    canvas.setFont("Helvetica", 8.5)
    canvas.drawRightString(w - 18 * mm, h - 18.5 * mm, subtitle)
    canvas.setFillColor(MUTED)
    canvas.setFont("Helvetica", 7.5)
    canvas.drawString(18 * mm, 10 * mm, f"Prepared by {AUTHOR} - {ORG_NAME} - aggregates only")
    canvas.drawRightString(w - 18 * mm, 10 * mm, f"Page {doc.page}")
    canvas.restoreState()


def build(state: dict, out_dir: Path = EXPORT_DIR) -> Path:
    """Write the PDF and return its path."""
    week = ((state.get("creative") or {}).get("channel_overview") or {}).get("week") or date.today().isoformat()
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"Weekly_Briefing_{week}.pdf"
    title = "Weekly marketing briefing"
    subtitle = f"Week of {day(week)} - generated {datetime.now().strftime('%d %b %Y %H:%M')}"
    doc = SimpleDocTemplate(str(path), pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm, topMargin=34 * mm,
                            bottomMargin=18 * mm, title=f"{ORG_NAME} - {title} - week of {day(week)}", author=AUTHOR)
    story = (briefing_section(state) + [Spacer(1, 10)] + plan_section(state) + [Spacer(1, 10)] + attention_section(state)
             + [Spacer(1, 10)] + consultation_section(state) + appendix(state))
    doc.build(story, onFirstPage=lambda c, d: _page(c, d, title, subtitle), onLaterPages=lambda c, d: _page(c, d, title, subtitle))
    return path
