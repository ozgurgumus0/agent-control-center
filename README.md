# Marketing Agent Control Center

A local, single-machine control center for a performance-marketing team. It
watches the data behind paid-media budget allocation, runs a set of small
"agents" over it, and shows their findings in one web interface next to the
budget planner (allocation tool) it supports.

The guiding rules are built into the code:

- **Read-only towards production.** Nothing here changes a budget, a campaign,
  the planner's state or any production system. Production writes, GitHub
  writes and external notifications are hard-wired off.
- **Aggregates only.** Agents return counts, shares and rates per channel,
  campaign, creative or week. Lead-level identifiers are dropped on read and
  never reach the UI, the MCP tools or an LLM.
- **Describe, never prescribe.** No agent recommends a budget or a money
  figure; the allocation stays a human decision in the planner.
- **LLM agents are opt-in and capped.** They need an API key *and* a switch in
  the UI, and are bounded by a per-run and a monthly cost ceiling.

## Architecture

```
            +-------------------- server.py (http://127.0.0.1:8765) --------------------+
 browser -> | static frontend (dist/)   JSON API (/api/state, /api/run, ...)            |
            | 30-second scheduler thread (optional daily monitoring run)                |
            +---------------------------------------------------------------------------+
                 |             |              |             |              |
         deterministic agents (no cost)                   LLM agents (opt-in)
   budget_pacing  creative_data/_insights  loi_feed  organic_leak_agent  plan_review   llm_agents
                                                                                        (Analyst, Investigator,
                                                                                         Context Scout)
                 |                                                         |
          briefing_pdf.py (weekly PDF)                     mcp_server.py (read-only MCP tools)
```

- **`server.py`** - a `ThreadingHTTPServer` on `127.0.0.1:8765`. Serves the
  frontend, exposes the JSON API, and starts a scheduler thread that checks
  every 30 seconds whether a daily monitoring run is due. A monitoring run calls
  the attribution project's CLI on the newest measurement table, then refreshes
  the agent caches.
- **Deterministic agents** (pure Python, no API cost):
  - `creative_data.py` / `creative_insights.py` - reads the creative ad
    performance Google Sheet (spend per ad, CRM funnel per creative) into a
    local cache and computes data checks, funnel breakdowns, creative fatigue,
    quality vs volume, creative attributes and product mix.
  - `budget_pacing.py` - snapshots platform budgets (via an external
    ads-automation project) and compares last week's spend with them.
  - `loi_feed.py` - consultation-quality aggregates from an LOI (lead quality)
    scoring workbook, and a calibration check of the signing rates the planner
    assumes per band.
  - `organic_leak_agent.py` - counts "organic"-labelled leads that arrived with
    a paid click id (gclid/gbraid/wbraid/fbclid) in a CRM export.
  - `plan_review.py` - reads the latest planner run (local backend API or an
    exported workbook) and attaches the other agents' comments per channel.
- **LLM agents** (`llm_agents.py`, Anthropic API):
  - *Analyst* - one structured call that turns the computed sections into a
    weekly briefing (3-5 findings).
  - *Incident Investigator* - one structured call that proposes a label for a
    monitoring incident; a human accepts, relabels or dismisses it.
  - *Context Scout* - a short tool loop with web search that proposes dated
    external events for the event registry; a human approves each one.
- **`briefing_pdf.py`** - builds the weekly briefing PDF (reportlab) from the
  same state the UI shows (`GET /api/briefing/pdf`).
- **`mcp_server.py`** - an MCP server named `marketing-agents` that exposes the
  agents' cached findings as read-only tools (`overview`, `allocation_plan`,
  `channel_overview`, `funnel_breakdown`, `creative_checks`, `creative_fatigue`,
  `quality_vs_volume`, `creative_attributes`, `product_mix`, `loi_quality`,
  `loi_calibration`, `call_reasons`, `incidents`, `latest_briefing`,
  `budget_pacing_check`).
- **Frontend** (`dist/`) - plain HTML/CSS/JS: an agent network view, overview,
  incidents review, policy editor, creative and LOI pages, and an embedded view
  of the planner's own frontend.

## Folder structure

```
agent-control-center/
  server.py                 HTTP server, JSON API, scheduler
  llm_agents.py             Analyst, Investigator, Context Scout (Anthropic API)
  creative_data.py          creative sheet fetch + local cache
  creative_insights.py      creative checks, funnel breakdown, briefing sections
  budget_pacing.py          platform budgets vs spend
  loi_feed.py               consultation-quality aggregates + calibration
  organic_leak_agent.py     organic leads with a paid click id
  plan_review.py            latest planner run + agent comments
  briefing_pdf.py           weekly briefing PDF
  mcp_server.py             read-only MCP server
  dist/                     frontend (index.html, app.js, styles.css)
  tests/                    offline unit tests (synthetic fixtures)
  runtime/                  created at run time: caches, logs, PID record (git-ignored)
  start_all.ps1             start planner backend + frontend + control center (Windows)
  stop_all.ps1              stop what start_all.ps1 recorded
  Start Control Center.cmd  double-click launcher for start_all.ps1
  requirements.txt
  .env.example
```

## External dependencies

The control center is a hub: most inputs come from sibling projects, located
through environment variables (see `.env.example`):

| Variable | What it points to |
| --- | --- |
| `ATTRIBUTION_PROJECT_ROOT` | attribution/monitoring project: measurement tables, monitoring CLI (`organic_attribution.cli`), incidents, policy, event registry |
| `PLANNER_ROOT`, `PLANNER_API_URL`, `PLANNER_EV_SQL`, `PLANNER_EXPORT_DIR` | the budget planner (backend + frontend), its EV SQL and exported plans |
| `ALLOCATOR_DATA` | allocator backtest extract used by the LLM tools |
| `ADS_AUTOMATION_ROOT` | ads-automation project providing `fetchers.budgets.fetch_budgets()`; it holds its own platform credentials |
| `GOOGLE_SHEETS_ID_PRODUCTION`, `GOOGLE_SERVICE_ACCOUNT_FILE` | creative ad performance sheet (read-only) |
| `LOI_OUTPUTS_DIR`, `CRM_EXPORT_GLOB` | LOI scoring outputs and the CRM export |

Every agent degrades gracefully when its source is missing: the UI shows the
agent as paused or "no data" instead of failing.

## Setup

Requires Python 3.11+.

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate   |   macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # then fill in the values you need
```

`ANTHROPIC_API_KEY` is only needed for the LLM agents, which also have to be
switched on in the UI (Policy page). Monthly and per-run cost ceilings are set
in `llm_agents.py` (`MONTHLY_BUDGET_USD`, `MAX_COST_USD`).

## Run

Control center only:

```bash
python server.py
# open http://127.0.0.1:8765
```

Everything (planner backend on :8000, planner frontend on :5173, control
center on :8765) on Windows:

```powershell
.\start_all.ps1            # or double-click "Start Control Center.cmd"
.\stop_all.ps1             # stop all three
.\stop_all.ps1 -Service planner-backend   # stop one
```

`stop_all.ps1` only stops PIDs recorded in `runtime/last-started-processes.json`
and only if each is still the process listening on that service's port. Logs go
to `runtime/<service>.log` and `runtime/<service>.err.log`.

### MCP server

```bash
python mcp_server.py      # stdio transport
```

Register it in your MCP client (e.g. a `.mcp.json` entry running
`python <path>/mcp_server.py`). Every tool is read-only and answers from the
caches written by the last "Run agents now".

## Tests

```bash
python -m unittest discover -s tests -v
```

Tests run offline on synthetic fixtures; none reaches Google Sheets, the
platform APIs or the Anthropic API. A few LLM-agent tests need a local
allocator extract or incident folder and are skipped when those are absent.
The MCP tests need the `mcp` package.

## Reading the attribution badge

The evidence gate emits three verdicts: `NOT_RUN` (grey), `UNRESOLVED` (amber)
and `DATA_BLOCKED` (red). There is deliberately no passing verdict -
prediction alone never unlocks EV adjustments; only an experiment can - so
amber is the best reachable state, not a warning.
