let state = null;
let selectedNodeId = "attribution";
let activeWorkspace = "attribution";
let activeAttributionView = "agents";   // Network is first in the menu and the landing view

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[char]));
}

function statusClass(status) {
  return `status-${String(status || "unknown").toLowerCase().replaceAll("_", "-")}`;
}

// The evidence gate emits exactly three verdicts: NOT_RUN when no
// classification file exists yet, and DATA_BLOCKED / UNRESOLVED from
// classify_observational_evidence. There is deliberately no passing verdict --
// prediction alone never unlocks EV -- so UNRESOLVED is the best reachable
// state and gets amber, not mint. Colouring everything red made a considered
// null result look like an outage.
const VERDICT_TONE = {
  DATA_BLOCKED: "blocked",
  UNRESOLVED: "danger",
  NOT_RUN: "muted",
};

function verdictTone(result) {
  return VERDICT_TONE[String(result || "NOT_RUN").toUpperCase()] ?? "danger";
}

function toast(message, error = false) {
  const node = $("#toast");
  node.textContent = message;
  node.className = `show${error ? " error" : ""}`;
  window.setTimeout(() => { node.className = ""; }, 3200);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || `Request failed: ${response.status}`);
  return payload;
}

function buildNetworkNodes(current) {
  const agents = Object.fromEntries((current.agents || []).map((agent) => [agent.id, agent]));
  const history = current.extended_history || {};
  const planner = current.planner || {};
  return [
    { id: "media", name: "Paid media", kind: "INPUT", status: "LIVE", role: "Daily channel spend and delivery signals.", detail: "Meta, Google, PMax, YouTube, Display, Outbrain and TikTok feed the measurement layer." },
    { id: "crm", name: "CRM + Organic", kind: "INPUT", status: "LIVE", role: "Lead, source and contract outcomes.", detail: `${current.measurement?.total_leads || 0} total leads and ${current.measurement?.organic_leads || 0} Organic leads in the current snapshot.` },
    { ...agents.context, id: "controls", kind: "AGENT" },
    { ...agents.watchdog, kind: "AGENT" },
    { ...agents.analyst, kind: "AGENT" },
    { ...agents.forensics, kind: "AGENT" },
    { ...agents.loi, kind: "AGENT" },
    { ...agents.organic_leak, kind: "AGENT" },
    { id: "history", name: "History Builder", kind: "AGENT", status: history.decision?.includes("GATE_MET") ? "RECONCILE" : "BUILDING", role: "Combines compatible historical observations.", detail: `${history.usable_complete_weeks || 0} complete weeks assembled. ${history.calendar_missing_weeks || 0} source-boundary weeks remain missing.` },
    { ...agents.attribution, kind: "GATE" },
    { ...agents.ev, kind: "VALUE" },
    { ...agents.planner, kind: "ALLOCATOR", detail: `${agents.planner?.detail || ""} Model: ${planner.model || "v6 CatBoost ensemble"}. Neutral parity: ${planner.neutral_parity_passed ? "passed" : "not verified"}.` },
    { ...agents.decision, kind: "OUTPUT" },
  ].filter((node) => node.id);
}

function graphLayout(narrow) {
  if (narrow) {
    return {
      viewBox: "0 0 560 1050",
      nodes: {
        media: [18, 30, 158, 80], crm: [201, 30, 158, 80], controls: [384, 30, 158, 80],
        watchdog: [194, 160, 172, 78], analyst: [384, 160, 158, 78], forensics: [68, 302, 172, 78], history: [320, 302, 172, 78],
        attribution: [184, 456, 192, 88], ev: [194, 616, 172, 78], planner: [184, 770, 192, 88], decision: [194, 928, 172, 78],
        loi: [18, 616, 158, 78], organic_leak: [384, 616, 158, 78],
      },
      vertical: true,
    };
  }
  return {
    viewBox: "0 0 1400 610",
    nodes: {
      media: [35, 55, 150, 80], crm: [35, 211, 150, 80], controls: [35, 367, 150, 80],
      watchdog: [285, 55, 175, 78], analyst: [535, 55, 195, 78], forensics: [285, 211, 175, 78], history: [285, 367, 175, 78],
      attribution: [535, 203, 195, 94], ev: [780, 207, 165, 86], planner: [990, 202, 180, 96], decision: [1215, 207, 165, 86],
      loi: [285, 505, 175, 78], organic_leak: [535, 367, 195, 78],
    },
    vertical: false,
  };
}

function edgePath(from, to, vertical) {
  const [fx, fy, fw, fh] = from;
  const [tx, ty, tw, th] = to;
  // Nodes stacked in one column (overlapping x) connect top/bottom even in the
  // wide layout; upward edges leave from the top and enter from the bottom.
  const stacked = fx < tx + tw && tx < fx + fw;
  if (vertical || stacked) {
    const up = fy > ty;
    const startX = fx + fw / 2;
    const startY = up ? fy : fy + fh;
    const endX = tx + tw / 2;
    const endY = up ? ty + th : ty;
    const bend = (up ? -1 : 1) * Math.max(34, Math.abs(endY - startY) * .52);
    return `M ${startX} ${startY} C ${startX} ${startY + bend}, ${endX} ${endY - bend}, ${endX} ${endY}`;
  }
  const startX = fx + fw;
  const startY = fy + fh / 2;
  const endX = tx;
  const endY = ty + th / 2;
  const bend = Math.max(46, (endX - startX) * .52);
  return `M ${startX} ${startY} C ${startX + bend} ${startY}, ${endX - bend} ${endY}, ${endX} ${endY}`;
}

function edgeState(from, to, current) {
  if (from === "organic_leak") return current.organic_leak?.status === "TRACED" ? "review" : "active";
  if (from === "loi") return current.loi?.status === "WARN" ? "review" : "active";
  if (["ev", "planner", "decision"].includes(to) && !current.planner?.shadow_allowed) return "blocked";
  if ((from === "forensics" || to === "forensics") && current.summary.open_incidents) return "review";
  if (from === "history" && current.extended_history?.calendar_missing_weeks) return "review";
  return "active";
}

// "Latest result": the agent's own checks as OK/LOOK rows where it has them, otherwise
// its detail text, with a long "a; b; c" list broken into bullets.
function inspectorResult(node, current) {
  const checks = { loi: current.loi?.checks, watchdog: current.creative?.watchdog?.checks }[node.id];
  if (checks?.length) return `<div class="insp-result"><p class="hyp2-kicker">LATEST RESULT</p>${creativeChecks({ checks })}</div>`;
  if (!node.detail) return "";
  const parts = String(node.detail).split(/;\s+/).filter(Boolean);
  const body = parts.length >= 3
    ? `<ul class="insp-list">${parts.map((p) => `<li>${prose(p)}</li>`).join("")}</ul>`
    : `<p class="insp-detail">${prose(node.detail)}</p>`;
  return `<div class="insp-result"><p class="hyp2-kicker">LATEST RESULT</p>${body}</div>`;
}

function renderInspector(root, node, current) {
  if (!root || !node) return;
  const planner = current.planner || {};
  let facts = "";
  if (node.id === "planner") {
    facts = `
      <div class="node-fact"><span>Model</span><strong>${escapeHtml(planner.model || "v6 CatBoost ensemble")}</strong></div>
      <div class="node-fact"><span>Commit</span><strong>${escapeHtml((planner.commit || "unknown").slice(0, 8))}</strong></div>
      <div class="node-fact"><span>Technical bridge</span><strong>${planner.neutral_parity_passed ? "PARITY PASSED" : "NOT VERIFIED"}</strong></div>
      <div class="node-fact"><span>Production writes</span><strong>DISABLED</strong></div>`;
  } else if (node.id === "attribution") {
    const blockers = (current.classification?.blockers || []).slice(0, 3);
    facts = `<ul class="node-blockers">${blockers.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>`;
  } else if (node.id === "analyst") {
    const llm = current.llm || {};
    const report = llm.analyst_report;
    facts = `
      <div class="node-fact"><span>Model</span><strong>${escapeHtml(llm.model || "")}</strong></div>
      <div class="node-fact"><span>API key</span><strong>${llm.key_present ? "PRESENT" : "MISSING"}</strong></div>
      <div class="node-fact"><span>LLM agents</span><strong>${llm.enabled ? "ENABLED" : "SWITCHED OFF"}</strong></div>
      <div class="node-fact"><span>Cost cap per run</span><strong>$${escapeHtml(llm.max_cost_usd?.analyst)}</strong></div>
      ${budgetFact(llm)}
      ${llm.last_error ? `<p class="node-detail">Last error: ${escapeHtml(llm.last_error)}</p>` : ""}
      ${analystReport(report, true)}`;
  } else if (node.id === "controls") {
    facts = `${budgetFact(current.llm || {})}${contextCandidates(current.llm?.context)}`;
  } else if (node.id === "forensics") {
    const hypotheses = current.llm?.hypotheses || {};
    const byId = Object.fromEntries((current.incidents || []).map((i) => [i.incident_id, i]));
    facts = Object.entries(hypotheses).map(([id, record]) => hypothesisCard(id, record, byId[id], true)).join("");
  } else if (node.id === "loi") {
    const loi = current.loi || {};
    const cal = loi.calibration || {};
    const movers = loi.reasons?.digest?.movers || [];
    facts = `
      <div class="node-fact"><span>Source</span><strong>${escapeHtml(loi.folder || "not computed")}</strong></div>
      <div class="node-fact"><span>Planner rates off</span><strong>${escapeHtml((cal.planner_outside_ci || []).length)}</strong></div>
      <div class="node-fact"><span>Readiness order</span><strong>${cal.ai_order_holds ? "HOLDS" : "BROKEN"}</strong></div>
      <div class="node-fact"><span>Call digest movers</span><strong>${escapeHtml(movers.length)}</strong></div>
`;
  } else if (node.id === "organic_leak") {
    const leak = current.organic_leak || {};
    facts = leak.organic_labelled ? `
      <div class="node-fact"><span>CRM export</span><strong>${escapeHtml(leak.export || "—")}</strong></div>
      <div class="node-fact"><span>Organic-labelled leads</span><strong>${escapeHtml(leak.organic_labelled)}</strong></div>
      <div class="node-fact"><span>Carry a paid click id</span><strong>${escapeHtml(leak.traced_to_paid)} (${(100 * (leak.traced_share || 0)).toFixed(1)}%)</strong></div>
      <div class="node-fact"><span>Meta / Google</span><strong>${escapeHtml(leak.by_platform?.meta ?? 0)} / ${escapeHtml(leak.by_platform?.google ?? 0)}</strong></div>
      <p class="node-detail">A floor, not a total: clicks that lost their id stay invisible. Proves contact with an ad, not that the ad caused the lead.</p>` : "";
  } else if (node.id === "history") {
    facts = `
      <div class="node-fact"><span>Complete weeks</span><strong>${escapeHtml(current.extended_history?.usable_complete_weeks || 0)}</strong></div>
      <div class="node-fact"><span>Missing boundary weeks</span><strong>${escapeHtml(current.extended_history?.calendar_missing_weeks || 0)}</strong></div>`;
  }
  root.innerHTML = `
    <div class="insp-head">
      <div><p class="eyebrow">${escapeHtml(node.kind || "NODE")}</p><h3>${escapeHtml(node.name)}</h3></div>
      <span class="agent-status ${statusClass(node.status)}"><i></i>${escapeHtml(node.status)}</span>
    </div>
    <p class="insp-role">${escapeHtml(node.role || "")}</p>
    ${inspectorResult(node, current)}
    ${facts}
    <div class="insp-actions">
    ${node.id === "watchdog" ? '<button class="secondary-button node-action" data-network-action="run">Run monitoring</button>' : ""}
    ${node.id === "forensics" ? '<button class="secondary-button node-action" data-network-action="incidents">Review incidents</button>' : ""}
    ${node.id === "loi" ? '<button class="secondary-button node-action" data-network-action="loi">Open LOI page</button>' : ""}
    ${node.id === "forensics" && Object.keys(current.llm?.hypotheses || {}).length ? `<button class="secondary-button node-action" data-llm-agent="investigator" ${llmReady(current) ? "" : "disabled"}>Investigate with ${escapeHtml(current.llm.model)}</button>` : ""}
    ${node.id === "analyst" ? `<button class="secondary-button node-action" data-llm-agent="analyst" ${llmReady(current) ? "" : "disabled"}>${current.llm?.running === "analyst" ? "Analyst running…" : `Run analyst (max $${escapeHtml(current.llm?.max_cost_usd?.analyst)})`}</button>` : ""}
    ${node.id === "controls" ? `<button class="secondary-button node-action" data-llm-agent="context" ${llmReady(current) ? "" : "disabled"}>${current.llm?.running === "context" ? "Scanning the web…" : `Scan the web (max $${escapeHtml(current.llm?.max_cost_usd?.context)})`}</button>` : ""}
    ${node.id === "planner" ? `<button class="secondary-button node-action" disabled>${planner.shadow_allowed ? "Shadow ready" : "Evidence gate locked"}</button>` : ""}
    </div>`;
  root.querySelectorAll("[data-llm-agent]").forEach((button) => button.addEventListener("click", async () => {
    const agent = button.dataset.llmAgent;
    const targets = agent === "investigator" ? Object.keys(current.llm?.hypotheses || {}) : [null];
    button.disabled = true;
    try {
      // One run at a time on the server; investigate open incidents one after another.
      for (const incident of targets) {
        await api("/api/llm/run", { method: "POST", body: JSON.stringify({ agent, incident_id: incident }) });
        toast(`${agent} started${incident ? ` on ${incident.slice(0, 24)}` : ""}.`);
        await waitForLlm();
      }
    } catch (error) { toast(error.message, true); }
    await loadState();
  }));
  const action = root.querySelector("[data-network-action]");
  if (action) action.addEventListener("click", () => {
    if (action.dataset.networkAction === "run") $("#runButton").click();
    if (action.dataset.networkAction === "incidents") $('[data-view="incidents"]').click();
    if (action.dataset.networkAction === "loi") $('[data-view="loi"]').click();
  });
}

const INCIDENT_LABELS = ["CONFIRMED_OPERATIONAL_CHANGE", "EXPECTED_VARIATION", "DATA_QUALITY_ISSUE", "UNRESOLVED"];

// Markdown tables and paragraphs from the briefing appendix, as HTML. Escaped first.
function markdownBlocks(md) {
  const lines = String(md || "").split(/\r?\n/);
  const out = [];
  let table = [];
  const flush = () => {
    if (!table.length) return;
    const rows = table.filter((l) => !/^\s*\|?\s*:?-{2,}/.test(l)).map((l) => l.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim()));
    const [head, ...body] = rows;
    out.push(`<div class="table-wrap"><table class="data-table"><thead><tr>${head.map((c) => `<th>${escapeHtml(c)}</th>`).join("")}</tr></thead><tbody>${body.map((r) => `<tr>${r.map((c) => `<td>${escapeHtml(c)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`);
    table = [];
  };
  for (const line of lines) {
    if (/^\s*\|/.test(line)) { table.push(line); continue; }
    flush();
    if (/^\s*#{1,6}\s/.test(line)) out.push(`<p class="sub-head">${escapeHtml(line.replace(/^\s*#+\s*/, ""))}</p>`);
    else if (line.trim()) out.push(`<p class="panel-note">${prose(line.trim())}</p>`);
  }
  flush();
  return out.join("");
}

const FOLLOW = { held: ["Held", "warn"], did_not_hold: ["Did not hold", "ok"], cannot_tell: ["Can't tell yet", "muted"] };

// What happened to last week's watch items.
function followUp(items) {
  if (!items?.length) return "";
  return `<section class="hyp-section check"><h5>Last week's watch items <span>${items.length}</span></h5>
      <ul class="follow-list">${items.map((f) => {
        const [text, tone] = FOLLOW[f.verdict] || [f.verdict, "muted"];
        return `<li><span class="tag ${tone}">${escapeHtml(text)}</span><div><strong>${escapeHtml(f.item)}</strong><p>${prose(f.note)}</p></div></li>`;
      }).join("")}</ul></section>`;
}

// compact = the Network inspector: headline, the finding titles, a link to the full briefing.
function analystReport(report, compact = false) {
  const record = realRecord(report);
  const out = record?.output;
  if (!record) return '<div class="hyp2 muted"><p class="hyp2-kicker">WEEKLY BRIEFING</p><p>No briefing written yet.</p></div>';
  if (record.status !== "COMPLETED" || !out) return `<div class="hyp2 muted"><p class="hyp2-kicker">WEEKLY BRIEFING</p><p>Last run: ${escapeHtml(record.status)}${record.error ? ` (${escapeHtml(record.error)})` : ""}.</p></div>`;
  const verdict = `<div class="hyp2-verdict">
      <span class="hyp2-label ok">Week of ${escapeHtml(dayText(record.generated_at_utc.slice(0, 10)))}</span>
      <span class="hyp2-by">written by ${escapeHtml(record.model)} · $${Number(record.cost_usd).toFixed(2)}</span>
    </div>`;
  const findings = out.findings || [];
  if (compact) {
    return `<div class="hyp2 compact"><p class="hyp2-kicker">WEEKLY BRIEFING</p>${verdict}
        <p class="hyp2-summary">${prose(out.headline)}</p>
        <ol class="finding-titles">${findings.map((f) => `<li>${escapeHtml(f.title)} <span class="finding-num">${escapeHtml(f.key_number)}</span></li>`).join("")}</ol>
        <button class="secondary-button" data-go-view="creative">Read the full briefing →</button></div>`;
  }
  return `<div class="hyp2">
      <p class="hyp2-kicker">WEEKLY BRIEFING</p>${verdict}
      <p class="hyp2-summary">${prose(out.headline)}</p>
      <div class="finding-list">${findings.map((f, i) => `
        <article class="finding">
          <div class="finding-head"><span class="finding-n">${i + 1}</span><h5>${escapeHtml(f.title)}</h5><span class="finding-num">${escapeHtml(f.key_number)}</span></div>
          <p>${prose(f.story)}</p>
        </article>`).join("")}</div>
      ${followUp(out.follow_up)}
      ${hypSection("Check next week", out.watch_items, "check", 5)}
      ${out.appendix_markdown ? `<details class="hyp2-appendix"><summary>Supporting numbers</summary>${markdownBlocks(out.appendix_markdown)}</details>` : ""}
      <p class="hyp2-note">A description of the week, not a budget recommendation: the plan is decided in Planner.</p>
    </div>`;
}

// The suggestion card. Only a click here changes the incident, and the click is
// the human decision; the agent never writes a label itself.
// ---------------------------------------------------------- investigator card
const LABEL_TEXT = {
  CONFIRMED_OPERATIONAL_CHANGE: "Operational change", EXPECTED_VARIATION: "Expected variation",
  DATA_QUALITY_ISSUE: "Data-quality issue", UNRESOLVED: "Unresolved",
};
const LABEL_TONE = { CONFIRMED_OPERATIONAL_CHANGE: "info", EXPECTED_VARIATION: "ok", DATA_QUALITY_ISSUE: "warn", UNRESOLVED: "muted" };
const CHANNEL_TEXT = { google: "Google", meta: "Meta", outbrain: "Outbrain", pmax: "PMax", youtube: "YouTube", display: "Display", tiktok: "TikTok", organic: "Organic" };

// "spend_eur_google" -> "Google spend"; falls back to the raw key.
function metricText(key) {
  const k = String(key || "");
  const channel = (suffix) => CHANNEL_TEXT[suffix] || suffix;
  let m;
  if ((m = k.match(/^spend_eur_(\w+)$/))) return `${channel(m[1])} spend`;
  if ((m = k.match(/^dashboard_leads_(\w+)$/))) return `${channel(m[1])} CRM leads`;
  if ((m = k.match(/^media_platform_leads_(\w+)$/))) return `${channel(m[1])} platform leads`;
  if (k === "total_leads") return "Total leads";
  if (k === "total_paid_spend_eur") return "Total paid spend";
  const text = k.replaceAll("_", " ");
  return text.charAt(0).toUpperCase() + text.slice(1);
}
const dayText = (day) => (day ? new Date(`${day}T12:00:00`).toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" }) : "");

// Numbers stand out so a long sentence can be scanned. Runs on already-escaped text.
const markNumbers = (html) => html.replace(/(^|[\s(€$])([+−-]?\d[\d.,]*%?)(?=[\s).,;:]|$)/g, '$1<span class="num">$2</span>');
const prose = (text) => markNumbers(escapeHtml(text));

function hypSection(title, items, kind, open = 3) {
  if (!items?.length) return "";
  const li = (item) => `<li>${prose(item)}</li>`;
  const tag = kind === "check" ? "ol" : "ul";
  const shown = items.slice(0, open).map(li).join("");
  const rest = items.slice(open);
  return `<section class="hyp-section ${kind}">
      <h5>${escapeHtml(title)} <span>${items.length}</span></h5>
      <${tag}>${shown}</${tag}>
      ${rest.length ? `<details><summary>Show ${rest.length} more</summary><${tag} start="${open + 1}">${rest.map(li).join("")}</${tag}></details>` : ""}
    </section>`;
}

// compact = the Network inspector: verdict, summary, first check, link to the full review.
function hypothesisCard(id, record, incident = null, compact = false) {
  const title = incident ? `${metricText(incident.key)} · ${dayText(incident.event_date)}` : id;
  if (!record) return `<div class="hyp2 muted"><p class="hyp2-title">${escapeHtml(title)}</p><p>Not investigated yet.</p></div>`;
  if (record.status !== "COMPLETED") return `<div class="hyp2 muted"><p class="hyp2-title">${escapeHtml(title)}</p><p>Last run: ${escapeHtml(record.status)}.</p></div>`;
  const out = record.output;
  const level = { low: 1, medium: 2, high: 3 }[out.confidence] || 0;
  const meter = `<span class="hyp2-meter" title="${escapeHtml(out.confidence)} confidence">${[1, 2, 3].map((i) => `<i class="${i <= level ? "on" : ""}"></i>`).join("")} ${escapeHtml(out.confidence)} confidence</span>`;
  const verdict = `<div class="hyp2-verdict">
      <span class="hyp2-label ${LABEL_TONE[out.suggested_label] || "muted"}">${escapeHtml(LABEL_TEXT[out.suggested_label] || out.suggested_label)}</span>${meter}
      <span class="hyp2-by">suggested by ${escapeHtml(record.model)} · $${Number(record.cost_usd).toFixed(2)}</span>
    </div>`;
  const review = record.review;
  const actions = review
    ? `<p class="hyp-reviewed">${review.action === "dismiss" ? "Suggestion dismissed" : `Label set: ${escapeHtml(LABEL_TEXT[review.label] || review.label)}`} · ${escapeHtml(review.at_utc.slice(0, 10))}</p>`
    : `<div class="hyp2-actions">
        <button class="primary-button" data-hyp-action="accept" data-hyp-id="${escapeHtml(id)}">Accept: ${escapeHtml(LABEL_TEXT[out.suggested_label] || out.suggested_label)}</button>
        <span class="hyp2-or">or</span>
        <select data-hyp-label="${escapeHtml(id)}" aria-label="Another label">${INCIDENT_LABELS.filter((l) => l !== out.suggested_label).map((l) => `<option value="${l}">${LABEL_TEXT[l]}</option>`).join("")}</select>
        <button class="secondary-button" data-hyp-action="relabel" data-hyp-id="${escapeHtml(id)}">Use this label</button>
        <button class="secondary-button ghost" data-hyp-action="dismiss" data-hyp-id="${escapeHtml(id)}">Dismiss</button>
        <p class="hyp2-note">Your click sets the incident's label. The agent only suggests.</p>
      </div>`;
  if (compact) {
    const first = (out.what_would_confirm || [])[0];
    return `<div class="hyp2 compact">
        <p class="hyp2-title">${escapeHtml(title)}</p>${verdict}
        <p class="hyp2-summary">${prose(out.hypothesis)}</p>
        ${first ? `<p class="hyp2-next"><b>First check:</b> ${prose(first)}</p>` : ""}
        <button class="secondary-button" data-go-view="incidents">Open the full review →</button>
      </div>`;
  }
  return `<div class="hyp2">
      <p class="hyp2-kicker">INVESTIGATOR'S SUGGESTION</p>
      ${verdict}
      <p class="hyp2-summary">${prose(out.hypothesis)}</p>
      <div class="hyp2-grid">
        ${hypSection("Why it thinks so", out.supporting_evidence, "for")}
        ${hypSection("What argues against it", out.contradicting_evidence, "against")}
      </div>
      ${hypSection("To confirm, check", out.what_would_confirm, "check", 4)}
      ${actions}
    </div>`;
}

document.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-hyp-action]");
  if (!button) return;
  const id = button.dataset.hypId;
  const action = button.dataset.hypAction;
  const body = { action };
  if (action === "relabel") body.label = button.parentElement.querySelector(`[data-hyp-label]`).value;
  button.disabled = true;
  try {
    await api(`/api/incidents/${encodeURIComponent(id)}/hypothesis`, { method: "POST", body: JSON.stringify(body) });
    toast(action === "dismiss" ? "Suggestion dismissed." : `Label ${action === "accept" ? "accepted" : "applied"}.`);
    await loadState();
  } catch (error) { toast(error.message, true); button.disabled = false; }
});

function budgetFact(llm) {
  return `<div class="node-fact"><span>LLM spend this month</span><strong>$${Number(llm.spent_this_month_usd || 0).toFixed(2)} / $${Number(llm.monthly_budget_usd || 0).toFixed(2)}</strong></div>`;
}

// Candidate external events. Only an Approve click writes the event registry.
const EVENT_TYPE_TEXT = { government_incentive: "Incentive", regulation: "Regulation", energy_price: "Energy prices",
  holiday: "Holiday", competitor: "Competitor", news_other: "News" };
const EFFECT = { demand_up: ["Demand up", "ok"], demand_down: ["Demand down", "warn"], unclear: ["Effect unclear", "muted"] };

// Candidate external events. Only an Approve click writes the event registry.
function contextCandidates(record) {
  record = realRecord(record);
  if (!record) return '<div class="hyp2 muted"><p class="hyp2-kicker">CONTEXT SCOUT</p><p>No web scan yet.</p></div>';
  if (record.status !== "COMPLETED") return `<div class="hyp2 muted"><p class="hyp2-kicker">CONTEXT SCOUT</p><p>Last scan: ${escapeHtml(record.status)}.</p></div>`;
  const events = record.output?.events || [];
  const open = events.filter((e) => !e.review).length;
  const verdict = `<div class="hyp2-verdict">
      <span class="hyp2-label ${open ? "warn" : "ok"}">${events.length} proposed · ${open} to review</span>
      <span class="hyp2-by">${escapeHtml(dayText(record.generated_at_utc.slice(0, 10)))} · ${escapeHtml(record.model)} · $${Number(record.cost_usd).toFixed(2)}</span>
    </div>`;
  if (!events.length) return `<div class="hyp2"><p class="hyp2-kicker">CONTEXT SCOUT</p>${verdict}<p class="hyp2-summary">The scan found no solid external events.</p></div>`;
  const cards = events.map((event, index) => {
    const [effectText, effectTone] = EFFECT[event.expected_effect] || [event.expected_effect, "muted"];
    const when = event.end_date && event.end_date !== event.start_date ? `${dayText(event.start_date)} → ${dayText(event.end_date)}` : dayText(event.start_date);
    return `<article class="event-card${event.review ? " reviewed" : ""}">
        <p class="event-when">${escapeHtml(when)}</p>
        <h5>${escapeHtml(event.title)}</h5>
        <div class="event-tags"><span class="tag">${escapeHtml(EVENT_TYPE_TEXT[event.event_type] || event.event_type)}</span><span class="tag">${escapeHtml(event.scope)}</span><span class="tag ${effectTone}">${escapeHtml(effectText)}</span></div>
        <p>${prose(event.reason)}</p>
        <p class="event-source">Source: <a href="${escapeHtml(event.source_url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(event.source_title || event.source_url)}</a></p>
        ${event.review
          ? `<p class="hyp-reviewed">${event.review.action === "approve" ? "Added to the event registry" : "Rejected"} · ${escapeHtml(event.review.at_utc.slice(0, 10))}</p>`
          : `<div class="hyp2-actions">
              <button class="primary-button" data-context-action="approve" data-context-run="${escapeHtml(record.run)}" data-context-index="${index}">Add to event registry</button>
              <button class="secondary-button ghost" data-context-action="reject" data-context-run="${escapeHtml(record.run)}" data-context-index="${index}">Reject</button>
            </div>`}
      </article>`;
  }).join("");
  return `<div class="hyp2"><p class="hyp2-kicker">CONTEXT SCOUT</p>${verdict}
      <p class="hyp2-summary">External events that could explain unusual days. Check the source before adding one: an added event is what the Watchdog and Investigator will cite.</p>
      <div class="event-list">${cards}</div></div>`;
}

document.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-context-action]");
  if (!button) return;
  button.disabled = true;
  try {
    await api("/api/context/review", { method: "POST", body: JSON.stringify({
      run: button.dataset.contextRun, index: Number(button.dataset.contextIndex), action: button.dataset.contextAction }) });
    toast(button.dataset.contextAction === "approve" ? "Event added to the registry." : "Event rejected.");
    await loadState();
  } catch (error) { toast(error.message, true); button.disabled = false; }
});

function llmReady(current) {
  const llm = current.llm || {};
  return llm.enabled && llm.key_present && !llm.running;
}

async function waitForLlm() {
  for (;;) {
    await new Promise((resolve) => setTimeout(resolve, 4000));
    const next = await api("/api/state");
    render(next);
    if (!next.llm?.running) {
      if (next.llm?.last_error) throw new Error(next.llm.last_error);
      return;
    }
  }
}

function renderNetwork(graphRoot, inspectorRoot, current) {
  if (!graphRoot) return;
  const nodes = buildNetworkNodes(current);
  const byId = Object.fromEntries(nodes.map((node) => [node.id, node]));
  const layout = graphLayout(graphRoot.clientWidth < 720);
  const connections = [
    ["media", "watchdog"], ["crm", "watchdog"], ["controls", "watchdog"],
    ["watchdog", "forensics"], ["watchdog", "history"], ["watchdog", "analyst"], ["forensics", "attribution"], ["history", "attribution"],
    ["attribution", "ev"], ["ev", "planner"], ["planner", "decision"],
    // The LOI Agent reads the consultations, feeds the briefing, and checks the
    // LOI signing rates Planner uses in EV (a report to people, not a value passed on).
    ["crm", "loi"], ["loi", "analyst"], ["loi", "planner"],
    // Observed paid clicks on "organic" leads: evidence the gate cannot block.
    ["crm", "organic_leak"], ["organic_leak", "attribution"],
  ];
  const paths = connections.map(([from, to], index) => {
    const path = edgePath(layout.nodes[from], layout.nodes[to], layout.vertical);
    const stateName = edgeState(from, to, current);
    return `<path id="edge-${index}" class="network-edge edge-${stateName}" d="${path}" marker-end="url(#arrow-${stateName})"></path>`;
  }).join("");
  const nodeMarkup = nodes.map((node) => {
    const [x, y, width, height] = layout.nodes[node.id];
    const selected = node.id === selectedNodeId ? " is-selected" : "";
    const stateName = statusClass(node.status);
    return `<g class="nn-node ${stateName}${selected}" data-node-id="${escapeHtml(node.id)}" role="button" tabindex="0" aria-label="${escapeHtml(node.name)}, ${escapeHtml(node.status)}">
      <rect class="node-halo" x="${x - 5}" y="${y - 5}" width="${width + 10}" height="${height + 10}" rx="18"></rect>
      <rect class="node-surface" x="${x}" y="${y}" width="${width}" height="${height}" rx="13"></rect>
      <circle class="node-neuron" cx="${x + 21}" cy="${y + 23}" r="7"></circle>
      <circle class="node-spark" cx="${x + 21}" cy="${y + 23}" r="2.5"></circle>
      <text class="node-kind" x="${x + 36}" y="${y + 20}">${escapeHtml(node.kind)}</text>
      <text class="node-label${node.name.length > 17 ? " long" : ""}" x="${x + 18}" y="${y + 48}">${escapeHtml(node.name)}</text>
      <text class="node-state" x="${x + 18}" y="${y + height - 10}">${escapeHtml(node.status)}</text>
    </g>`;
  }).join("");
  graphRoot.innerHTML = `<svg viewBox="${layout.viewBox}" role="img" aria-label="Data flows through monitoring, attribution and EV into the Planner CatBoost allocator. Locked connections cannot pass production value.">
    <defs>
      <filter id="node-glow" x="-60%" y="-60%" width="220%" height="220%"><feGaussianBlur stdDeviation="6" result="blur"></feGaussianBlur><feMerge><feMergeNode in="blur"></feMergeNode><feMergeNode in="SourceGraphic"></feMergeNode></feMerge></filter>
      <marker id="arrow-active" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0,0 L8,4 L0,8 Z"></path></marker>
      <marker id="arrow-review" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0,0 L8,4 L0,8 Z"></path></marker>
      <marker id="arrow-blocked" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0,0 L8,4 L0,8 Z"></path></marker>
    </defs>
    <g class="network-stars">${Array.from({ length: 36 }, (_, index) => `<circle cx="${(index * 83) % (layout.vertical ? 540 : 1380) + 10}" cy="${(index * 137) % (layout.vertical ? 1030 : 480) + 10}" r="${index % 3 === 0 ? 1.2 : .7}"></circle>`).join("")}</g>
    <g class="network-connections">${paths}</g>
    <g class="network-nodes">${nodeMarkup}</g>
  </svg>`;
  graphRoot.querySelectorAll("[data-node-id]").forEach((element) => {
    const select = () => {
      selectedNodeId = element.dataset.nodeId;
      renderNetworks(current);
    };
    element.addEventListener("click", select);
    element.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") select(); });
  });
  renderInspector(inspectorRoot, byId[selectedNodeId] || byId.attribution, current);
}

function renderNetworks(current) {
  renderNetwork($("#networkGraph"), $("#nodeInspector"), current);
  renderNetwork($("#agentNetwork"), $("#agentInspector"), current);
}

function renderRuns(runs) {
  const root = $("#runTimeline");
  if (!runs.length) {
    root.innerHTML = '<div class="empty-state">No monitoring runs yet.</div>';
    return;
  }
  root.innerHTML = runs.slice(0, 5).map((run) => `
    <div class="timeline-item">
      <strong>${escapeHtml(run.run_id || "monitoring run")}</strong>
      <span>${escapeHtml(run.watchdog_status || run.status || "completed")} · ${escapeHtml(run.analysis_as_of || "")}</span>
    </div>`).join("");
}

function renderIncidents(incidents, llm = {}) {
  const root = $("#incidentList");
  if (!incidents.length) {
    root.innerHTML = '<div class="empty-state">No local incidents. The review queue is clear.</div>';
    return;
  }
  root.innerHTML = incidents.map((incident) => {
    const evidence = incident.current_evidence || {};
    const facts = [
      ["Observed", evidence.observed != null ? Number(evidence.observed).toLocaleString("en-GB", { maximumFractionDigits: 2 }) : null],
      ["Usual (same weekday)", evidence.same_weekday_median != null ? Number(evidence.same_weekday_median).toLocaleString("en-GB", { maximumFractionDigits: 2 }) : null],
      ["How unusual (robust z)", evidence.robust_z != null ? Number(evidence.robust_z).toFixed(1) : null],
    ].filter(([, v]) => v != null);
    return `
      <article class="panel incident-card">
        <div class="incident-head">
          <div>
            <span class="agent-status ${statusClass(incident.status)}"><i></i>${escapeHtml(incident.status)}</span>
            <h3>${escapeHtml(metricText(incident.key))} · ${escapeHtml(dayText(incident.event_date))}</h3>
            <p class="incident-id">${escapeHtml(incident.incident_id)}</p>
          </div>
          <div class="incident-actions">
            <select aria-label="Incident status" data-incident-status="${escapeHtml(incident.incident_id)}">
              ${["OPEN", "INVESTIGATING", "RESOLVED", "DISMISSED"].map((status) => `<option ${status === incident.status ? "selected" : ""}>${status}</option>`).join("")}
            </select>
            <button class="secondary-button" data-save-incident="${escapeHtml(incident.incident_id)}">Save status</button>
          </div>
        </div>
        ${facts.length ? `<div class="incident-facts">${facts.map(([k, v]) => `<div><span>${escapeHtml(k)}</span><strong>${escapeHtml(v)}</strong></div>`).join("")}</div>` : ""}
        ${incident.incident_id in (llm.hypotheses || {}) ? hypothesisCard(incident.incident_id, realRecord(llm.hypotheses[incident.incident_id]), incident) : ""}
      </article>`;
  }).join("");
  $$('[data-save-incident]').forEach((button) => button.addEventListener("click", async () => {
    const id = button.dataset.saveIncident;
    const select = $(`[data-incident-status="${CSS.escape(id)}"]`);
    try {
      await api(`/api/incidents/${encodeURIComponent(id)}/status`, { method: "POST", body: JSON.stringify({ status: select.value }) });
      toast("Incident status saved locally.");
      await loadState();
    } catch (error) { toast(error.message, true); }
  }));
}

function renderPolicy(policy) {
  const form = $("#policyForm");
  Object.entries(policy || {}).forEach(([key, value]) => {
    const input = form.elements.namedItem(key);
    if (input && typeof value !== "object") input.value = value;
  });
}

function renderPlannerRuntime(current) {
  const runtime = current.planner?.runtime || {};
  const ready = Boolean(runtime.backend && runtime.frontend);
  const partial = Boolean(runtime.backend || runtime.frontend);
  $("#plannerRuntimeDot").className = `runtime-dot ${ready ? "ready" : partial ? "partial" : "offline"}`;
  $("#plannerRuntimeLabel").textContent = ready ? "Planner is ready" : partial ? "Planner is starting" : "Planner is offline";
  $("#plannerRuntimeDetail").textContent = `Backend ${runtime.backend ? "online" : "offline"} · interface ${runtime.frontend ? "online" : "offline"}`;
  $("#plannerOffline").hidden = ready;
  $("#plannerFrameWrap").hidden = !ready;
  const frame = $("#plannerFrame");
  if (ready && frame.src === "about:blank") frame.src = runtime.frontend_url || "http://localhost:5173";
}

// The weekly panel is two different sources spliced together, not one run of
// observations: a 2025 EV-model cohort table and the 2026 daily measurement
// table aggregated to Sunday weeks, with weeks in between that neither covers
// and no overlapping period to check one against the other.
//
// It used to render as `N / M weeks` over a bar pinned at 100%, which reads
// as a cleared gate. The count gate IS met -- that part was true -- but the
// number is spliced, so the bar is drawn as its three parts and the seam is
// stated instead of being averaged away.
function renderHistory(next) {
  const extended = next.extended_history || {};
  const boundary = extended.source_boundary || {};
  const gapWeeks = (boundary.missing_week_endings_between_sources || []).length;
  const legacy = extended.legacy_weeks ?? 0;
  const current = extended.current_weeks ?? (next.summary.current_weekly_observations ?? 0);
  const weeks = next.summary.weekly_observations ?? 0;
  const span = legacy + gapWeeks + current;

  if (span > 0) {
    const pct = (part) => `${(part / span) * 100}%`;
    $("#historyLegacy").style.width = pct(legacy);
    $("#historyGap").style.width = pct(gapWeeks);
    $("#historyCurrent").style.width = pct(current);
  }

  $("#historyWeeks").textContent = legacy && current
    ? `${weeks} weeks · ${legacy} legacy + ${current} current`
    : `${weeks} weeks`;
  $("#historyGateNote").textContent = `minimum history gate: 52 · ${weeks >= 52 ? "count met" : "count not met"}`;

  const seam = $("#historySeam");
  if (gapWeeks > 0) {
    const first = boundary.missing_week_endings_between_sources[0];
    const last = boundary.missing_week_endings_between_sources[gapWeeks - 1];
    seam.textContent =
      `SEAM · ${gapWeeks} week(s) covered by neither source (${first} → ${last}). ` +
      `Legacy ends ${boundary.last_legacy_week}, current starts ${boundary.first_current_week}: ` +
      `the sources do not overlap, so they cannot be checked against each other, ` +
      `and the 2025 and 2026 columns are different measurement objects. ` +
      `Treat ${weeks} as spliced, not as ${weeks} comparable observations.`;
    seam.hidden = false;
  } else {
    seam.hidden = true;
  }
}

// ---------------------------------------------------------------- creative
// Everything here is computed by creative_insights.py and only displayed.

const fmtEur = (v) => (v == null ? "—" : `€${Number(v).toLocaleString("en-GB", { maximumFractionDigits: v >= 100 ? 0 : 2 })}`);
const fmtNum = (v, d = 0) => (v == null ? "—" : Number(v).toLocaleString("en-GB", { maximumFractionDigits: d }));
const fmtPct = (v, d = 1) => (v == null ? "—" : `${Number(v).toFixed(d)}%`);
const fmtDelta = (v) => (v == null ? "—" : `${v > 0 ? "+" : ""}${Number(v).toFixed(1)}%`);

// columns: [key, label, formatter, deltaTone] -- deltaTone "cost" colours a rise amber.
function dataTable(columns, rows) {
  if (!rows?.length) return '<p class="panel-note">Nothing to show.</p>';
  const head = columns.map(([, label]) => `<th>${escapeHtml(label)}</th>`).join("");
  const body = rows.map((row) => `<tr>${columns.map(([key, , format, tone]) => {
    const value = row[key];
    const cls = tone === "cost" && value != null ? (value > 0 ? "up" : value < 0 ? "down" : "") : "";
    return `<td class="${cls}">${escapeHtml(format ? format(value) : value ?? "—")}</td>`;
  }).join("")}</tr>`).join("");
  return `<div class="table-wrap"><table class="data-table"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
}

function panel(eyebrow, title, body, extraClass = "") {
  return `<article class="panel ${extraClass}"><div class="panel-head"><div><p class="eyebrow">${escapeHtml(eyebrow)}</p><h2>${escapeHtml(title)}</h2></div></div>${body}</article>`;
}

function creativeChecks(watchdog) {
  const checks = watchdog?.checks || [];
  if (!checks.length) return '<p class="panel-note">No checks computed yet.</p>';
  return checks.map((check) => {
    const items = (check.items || []).slice(0, 5).map((item) => `<li>${escapeHtml(Object.values(item).join(" · "))}</li>`).join("");
    return `<div class="check-row">
      <div class="rf-head"><strong>${escapeHtml(check.title)}</strong><span class="badge ${check.status === "warn" ? "danger" : "ready"}">${check.status === "warn" ? "LOOK" : "OK"}</span></div>
      <p>${escapeHtml(check.detail)}</p>${items ? `<ul class="check-items">${items}</ul>` : ""}
    </div>`;
  }).join("");
}

function renderCreative(current) {
  const creative = current.creative || {};
  const data = creative.data;
  $("#creativeFreshness").textContent = data
    ? `Sheet fetched ${new Date(data.fetched_at_utc).toLocaleString([], { dateStyle: "medium", timeStyle: "short" })} · spend to ${data.tabs?.spend?.max_date ?? "—"} · funnel to ${data.tabs?.funnel?.max_date ?? "—"}`
    : "No creative data yet: fetch the sheet.";
  const briefingButton = $("#creativeBriefing");
  const llm = current.llm || {};
  briefingButton.disabled = !llmReady(current) || !data;
  briefingButton.textContent = llm.running === "analyst" ? "Writing briefing…" : `Write briefing (max $${llm.max_cost_usd?.analyst ?? "—"})`;
  briefingButton.title = !llm.enabled ? "LLM agents are switched off in the control state." : (!llm.key_present ? "No API key." : "");
  if (!data) { $("#creativeContent").innerHTML = '<div class="empty-state wide">Fetch the sheet to compute the creative sections.</div>'; return; }

  const overview = creative.channel_overview || {};
  const fatigue = creative.fatigue || {};
  const quality = creative.quality_vs_volume || {};
  const attrs = creative.attributes || {};
  const mix = creative.product_mix || {};
  const briefing = realRecord(llm.analyst_report)?.status === "COMPLETED"
    ? analystReport(llm.analyst_report)
    : `<p class="panel-note">${realRecord(llm.analyst_report) ? `Last run: ${escapeHtml(llm.analyst_report.status)}.` : "No briefing written yet."} The sections below are the data it reads.</p>`;

  const qualityBody = Object.entries(quality.channels || {}).map(([channel, block]) => `
    <p class="sub-head">${escapeHtml(channel)} · ${block.creatives} creatives · median CPL ${fmtEur(block.median_cpl_crm_eur)} · median cost/SC1 ${fmtEur(block.median_cost_per_sc1_eur)}</p>
    <p class="report-label">CHEAP LEADS, WEAK QUALITY</p>
    ${dataTable([["ad", "Creative"], ["spend_eur", "Spend", fmtEur], ["cpl_crm_eur", "CPL", fmtEur], ["cost_per_sc1_eur", "Cost/SC1", fmtEur], ["sc1_rate_pct", "SC1 rate", fmtPct], ["low_prio_share_pct", "Low prio", fmtPct]], block.cheap_leads_weak_quality)}
    <p class="report-label">DEARER LEADS, STRONG QUALITY</p>
    ${dataTable([["ad", "Creative"], ["spend_eur", "Spend", fmtEur], ["cpl_crm_eur", "CPL", fmtEur], ["cost_per_sc1_eur", "Cost/SC1", fmtEur], ["sc1_rate_pct", "SC1 rate", fmtPct], ["low_prio_share_pct", "Low prio", fmtPct]], block.dear_leads_strong_quality)}`).join("");

  const attrBody = Object.entries(attrs.attributes || {}).map(([name, values]) => `
    <p class="sub-head">${escapeHtml(name.replaceAll("_", " "))}</p>
    ${dataTable([["value", "Value"], ["creatives", "Ads", fmtNum], ["spend_eur", "Spend", fmtEur], ["ctr_pct", "CTR", (v) => fmtPct(v, 2)], ["cpl_crm_eur", "CPL", fmtEur], ["sc1_rate_pct", "SC1 rate", fmtPct], ["low_prio_share_pct", "Low prio", fmtPct]], values)}`).join("");

  const mixBody = Object.entries(mix.by || {}).map(([name, rows]) => `
    <p class="sub-head">By ${escapeHtml(name)}</p>
    ${dataTable([["value", name], ["spend_share_pct", "Spend", fmtPct], ["crm_leads_share_pct", "CRM leads", fmtPct], ["sc1_net_share_pct", "SC1", fmtPct], ["contracts_share_pct", "Contracts", fmtPct]], rows)}`).join("");

  $("#creativeContent").innerHTML = [
    panel("WEEKLY BRIEFING", "This week in one read", briefing, "wide"),
    panel("DATA WATCHDOG", `Creative data checks · ${creative.watchdog?.status ?? "—"}`, creativeChecks(creative.watchdog)),
    panel("CHANNELS", `Week of ${overview.week ?? "—"} vs the 4 before`,
      dataTable([["channel", "Channel"], ["spend_eur", "Spend", fmtEur], ["spend_change_pct", "Δ", fmtDelta], ["crm_leads", "CRM leads", fmtNum], ["crm_leads_change_pct", "Δ", fmtDelta], ["cpl_crm_eur", "CPL", fmtEur], ["cpl_crm_change_pct", "Δ", fmtDelta, "cost"]], overview.channels)
      + '<p class="panel-note">Whole-channel totals. CPL = spend per CRM lead.</p>'),
    panel("CREATIVE FATIGUE", `${fatigue.flagged ?? 0} of ${fatigue.creatives_checked ?? 0} long-running creatives flagged`,
      dataTable([["ad", "Creative"], ["channel", "Channel"], ["weeks_running", "Weeks", fmtNum], ["ctr_first2_pct", "CTR start", (v) => fmtPct(v, 2)], ["ctr_last2_pct", "CTR now", (v) => fmtPct(v, 2)], ["cpm_change_pct", "CPM Δ", fmtDelta, "cost"], ["spend_last2_eur", "Spend 2w", fmtEur]], fatigue.top)
      + `<p class="panel-note">${escapeHtml(fatigue.rule || "")}. ${escapeHtml(fatigue.note || "")}</p>`),
    panel("QUALITY VS VOLUME", `Cheap leads are not always good leads · ${quality.window ?? ""}`, qualityBody || '<p class="panel-note">No creative met the spend and lead floor.</p>'),
    panel("CREATIVE ATTRIBUTES", `What the Meta creatives have in common · ${attrs.creatives_with_attributes ?? 0} tagged`,
      (attrBody || '<p class="panel-note">Too few tagged creatives per value.</p>') + `<p class="panel-note">${escapeHtml(attrs.note || "")}</p>`),
    panel("PRODUCT MIX", `Spend vs results by product · ${fmtPct(mix.spend_with_product_tag_pct)} of spend tagged`,
      (mixBody || '<p class="panel-note">No tagged spend.</p>') + `<p class="panel-note">${escapeHtml(mix.note || "")} Meta and TikTok only: the channels whose leads tie to an ad.</p>`, "wide"),
  ].join("");
}

function renderLoi(current) {
  const loi = current.loi || {};
  $("#loiFreshness").textContent = loi.file
    ? `${loi.folder} · file saved ${loi.file_modified?.replace("T", " ")} · ${loi.status}`
    : "LOI feed not computed yet: recompute from the newest file.";
  if (!loi.file) { $("#loiContent").innerHTML = '<div class="empty-state wide">No LOI feed yet.</div>'; return; }
  const cal = loi.calibration || {};
  const weekly = loi.weekly_quality || {};
  const calCols = [["band", "Band"], ["calls", "Mature calls", fmtNum], ["rate_pct", "Signed", fmtPct],
    ["ci95_pct", "95% interval", (v) => (v ? `${v[0]}–${v[1]}%` : "—")], ["planner_pct", "Planner assumes", fmtPct],
    ["planner_inside_ci", "Match", (v) => (v == null ? "—" : v ? "yes" : "NO")]];
  $("#loiContent").innerHTML = [
    panel("LOI WATCHDOG", `Checks · ${loi.status}`, creativeChecks({ checks: loi.checks })),
    panel("CONSULTATION QUALITY", `Week of ${weekly.through_week ?? "—"} vs the 4 before`,
      dataTable([["metric", "Metric"], ["now", "This week", (v) => fmtNum(v, 1)], ["prior_4w", "Prior 4w", (v) => fmtNum(v, 1)], ["points", "Δ points", fmtDelta]],
        Object.entries(weekly.last_vs_prior_4w || {}).map(([metric, v]) => ({ metric: metric.replaceAll("_", " "), ...v })))
      + `<p class="panel-note">${escapeHtml(weekly.note || "")}</p>`),
    panel("PLANNER CALIBRATION", `Does each LOI band sign at the rate Planner assumes? · until ${cal.mature_until ?? "—"}`,
      `<p class="report-label">OUR AI LOI (USED FIRST)</p>${dataTable(calCols, cal.ai_bands)}`
      + `<p class="report-label">SELLER LOI (FALLBACK FOR UNSCORED LEADS)</p>${dataTable(calCols, cal.seller_fallback)}`
      + `<p class="panel-note">${escapeHtml(cal.note || "")} Rates read from the planner's ${escapeHtml(cal.planner_source || "")}.</p>`, "wide"),
    panel("WEEKLY FLOW", "Meetings, scoring and quality per week",
      dataTable([["week", "Week"], ["meetings", "Meetings", fmtNum], ["cancelled_pct", "Cancelled", fmtPct], ["no_recording_pct", "No recording", fmtPct],
        ["scored", "Scored", fmtNum], ["mean_readiness", "Readiness", (v) => fmtNum(v, 1)], ["hot_pct", "Hot (≥70)", fmtPct], ["negative_pct", "Non idoneo/int.", fmtPct]], weekly.weeks), "wide"),
    (() => {
      const digest = loi.reasons?.digest;
      if (!digest?.items?.length) return panel("LAST WEEK IN THE CALLS", "Not enough consultations yet", `<p class="panel-note">${escapeHtml(digest?.note || "No digest yet. In the LOI scoring project run: python -m src.export_reason_aggregates")}</p>`, "wide");
      const groups = [...new Set(digest.items.map((i) => i.group))];
      const movers = digest.movers.length
        ? dataTable([["name", "What moved"], ["group", "Group"], ["share_pct", "Last week", fmtPct], ["prior_4w_pct", "Prior 4w", fmtPct], ["points", "Δ points", fmtDelta]], digest.movers)
        : '<p class="panel-note">Nothing moved significantly against the 4 weeks before.</p>';
      const tables = groups.map((group) => `<p class="sub-head">${escapeHtml(group.replaceAll("_", " "))}</p>`
        + dataTable([["name", "Mentioned"], ["share_pct", "Last week", fmtPct], ["prior_4w_pct", "Prior 4w", fmtPct], ["points", "Δ points", fmtDelta]],
          digest.items.filter((i) => i.group === group))).join("");
      return panel("LAST WEEK IN THE CALLS", `Week of ${digest.week} · ${digest.consultations} consultations`,
        `<p class="report-label">MOVERS · ${escapeHtml(digest.rule || "")}</p>${movers}${tables}<p class="panel-note">${escapeHtml(digest.note || "")}</p>`, "wide");
    })(),
    panel("WHAT CUSTOMERS SAY", `Validated markers per week · through ${loi.reasons?.through_week ?? "—"}`,
      loi.reasons
        ? dataTable([["week", "Week"], ["consultations", "Consultations", fmtNum],
            ...Object.entries(loi.reasons.definitions).filter(([, d]) => d.effect_on_signing !== "not validated")
              .map(([key, d]) => [key, `${d.label} (${d.effect_on_signing})`, fmtPct])],
            (loi.reasons.weeks || []).slice(-8).map((w) => ({ week: w.week, consultations: w.consultations, ...w.share_pct })))
          + `<p class="panel-note">${escapeHtml(loi.reasons.note)} ${escapeHtml(loi.reasons.condition || "")}.</p>`
        : '<p class="panel-note">No reason feed yet. In the LOI scoring project run: python -m src.export_reason_aggregates</p>', "wide"),
    panel("BY CHANNEL", `Historical · ${loi.by_channel?.window ?? "—"}`,
      dataTable([["channel", "Channel"], ["scored_calls", "Scored", fmtNum], ["mean_readiness", "Readiness", (v) => fmtNum(v, 1)], ["hot_pct", "Hot", fmtPct],
        ["negative_pct", "Non idoneo/int.", fmtPct], ["signed_net_pct", "Signed", fmtPct]], loi.by_channel?.channels)
      + `<p class="panel-note">${escapeHtml(loi.by_channel?.note || "")}</p>`, "wide"),
  ].join("");
}

// ------------------------------------------------------------ overview: results
// Everything below reads /api/state only; it adds no logic the agents do not have.

// A warning names the problem, not the check: the check titles state the healthy case.
const PROBLEM = {
  freshness: "Creative data is out of date", tracking_gap: "Platform and CRM leads drifted apart",
  spend_without_crm_leads: "Creative(s) spending without reaching the CRM", non_marketing_spend: "Non-marketing spend inside main_metrics",
  untagged_creatives: "Spending creatives missing from Ad Details", loi_freshness: "LOI scores are out of date",
  loi_channel_tags: "Recent scored calls have no channel or region", loi_unscored_share: "Many held meetings are not scored",
  loi_reasons_fresh: "The call-reason feed is out of date",
};
const problemOf = (check) => PROBLEM[check.id] || check.title;
// Layout samples are for the UI only; they are never a real result.
const realRecord = (record) => (record && !record.sample ? record : null);

const daysAgo = (day) => (day ? Math.round((Date.now() - new Date(`${day}T12:00:00`)) / 86400000) : null);

function renderFreshness(current) {
  $("#freshnessChips").innerHTML = (current.freshness || []).map((source) => `
    <span class="fresh-chip${source.stale ? " stale" : ""}" title="${escapeHtml(source.label)}: ${escapeHtml(source.date || "never")}">
      ${escapeHtml(source.label)} <strong>${escapeHtml(source.date ? source.date.slice(5) : "—")}</strong>${source.age_days != null ? ` · ${source.age_days}d` : ""}
    </span>`).join("");
}

// The attention list: what a person should look at, each with where to look.
function attentionItems(current) {
  const items = [];
  const open = (current.incidents || []).filter((i) => !["RESOLVED", "DISMISSED"].includes(i.status));
  open.forEach((incident) => {
    const hyp = realRecord(current.llm?.hypotheses?.[incident.incident_id]);
    items.push({ tone: "warn", text: `Incident to label: ${incident.key} on ${incident.event_date}`,
      sub: hyp?.status === "COMPLETED" ? `The Investigator suggests ${hyp.output.suggested_label.replaceAll("_", " ").toLowerCase()} (${hyp.output.confidence} confidence).` : "No Investigator hypothesis yet.",
      go: { view: "incidents" } });
  });
  const cal = current.loi?.calibration;
  if (cal?.planner_outside_ci?.length) items.push({ tone: "warn", text: `${cal.planner_outside_ci.length} LOI signing rate(s) in Planner differ from what actually signed`,
    sub: `${cal.planner_outside_ci.join(", ")}. Planner uses these inside EV: a decision for the Planner owner.`, go: { view: "loi" } });
  (current.creative?.watchdog?.checks || []).filter((c) => c.status === "warn").forEach((check) => items.push({ tone: "warn", text: problemOf(check), sub: check.detail, go: { view: "creative" } }));
  (current.loi?.checks || []).filter((c) => c.status === "warn" && c.id !== "loi_calibration").forEach((check) => items.push({ tone: "warn", text: problemOf(check), sub: check.detail, go: { view: "loi" } }));
  const pacing = current.pacing || {};
  const under = (pacing.campaigns || []).filter((c) => c.verdict === "under_delivering");
  const limited = (pacing.campaigns || []).filter((c) => c.verdict === "budget_limited");
  if (under.length) items.push({ tone: "warn", text: `${under.length} campaign(s) not spending their budget`,
    sub: `${under.slice(0, 3).map((c) => `${c.campaign} (${c.utilisation_pct}% of EUR ${c.daily_budget_eur}/day)`).join("; ")}${pacing.provisional ? ". Provisional until next week's snapshot." : "."}`, go: { view: "overview" } });
  if (limited.length) items.push({ tone: "info", text: `${limited.length} campaign(s) limited by their budget`,
    sub: `${limited.slice(0, 3).map((c) => c.campaign).join("; ")}: they would spend more if allowed.`, go: { view: "overview" } });
  const movers = current.loi?.reasons?.digest?.movers || [];
  if (movers.length) items.push({ tone: "info", text: `Last week in the calls: ${movers.map((m) => `${m.name} ${m.points > 0 ? "up" : "down"} ${Math.abs(m.points)} pts`).join(", ")}`,
    sub: "Significant shift vs the 4 weeks before. It says the conversation changed, not why.", go: { view: "loi" } });
  (current.freshness || []).filter((f) => f.stale).forEach((f) => items.push({ tone: "warn", text: `${f.label} is ${f.age_days == null ? "missing" : `${f.age_days} days old`}`,
    sub: { measurement: "The Data Watchdog's measurement checks stop there.", crm_export: "Ask the CRM owner for a fresh CRM export: the Leak Tracer and per-channel LOI depend on it.",
      creative: "Run agents now fetches it.", budgets: "Run agents now takes a new snapshot.", loi: "Recompute on the LOI page after the daily LOI run.", reasons: "In the LOI scoring project run: python -m src.export_reason_aggregates" }[f.id] || "", go: null }));
  if (current.llm && !current.llm.enabled) items.push({ tone: "info", text: "The LLM agents are switched off",
    sub: "Weekly Briefing, Investigator hypotheses and Context Scout need them. About $0.10–0.20 a run, within the $3 monthly cap.", go: null });
  return items;
}

function renderAttention(current) {
  const items = attentionItems(current);
  const warn = items.filter((i) => i.tone === "warn").length;
  $("#attentionCount").textContent = warn ? `${warn} TO LOOK AT` : "ALL CLEAR";
  $("#attentionCount").className = `badge ${warn ? "danger" : "ready"}`;
  $("#attentionList").innerHTML = items.length ? items.map((item, index) => `
    <div class="attention-item ${item.tone}"><span class="dot"></span>
      <p>${escapeHtml(item.text)}${item.sub ? `<small>${escapeHtml(item.sub)}</small>` : ""}</p>
      ${item.go ? `<button class="secondary-button" data-go-view="${escapeHtml(item.go.view || "")}" data-go-node="${escapeHtml(item.go.node || "")}">Open</button>` : ""}
    </div>`).join("") : '<p class="panel-note">Nothing needs a look right now.</p>';
}

// One box per agent. `finding` is the one line that matters; `go` is where to read more.
function resultCards(current) {
  const agents = Object.fromEntries((current.agents || []).map((a) => [a.id, a]));
  const fresh = Object.fromEntries((current.freshness || []).map((f) => [f.id, f]));
  const metaFor = (id, label) => { const f = fresh[id]; return f ? { text: `${label} ${f.date || "—"}${f.age_days != null ? ` · ${f.age_days}d old` : ""}`, stale: f.stale } : { text: "", stale: false }; };
  const run = (current.runs || [])[0] || {};
  const creativeWarn = (current.creative?.watchdog?.checks || []).filter((c) => c.status === "warn");
  const open = (current.incidents || []).filter((i) => !["RESOLVED", "DISMISSED"].includes(i.status));
  const hyps = Object.values(current.llm?.hypotheses || {}).filter((h) => realRecord(h)?.status === "COMPLETED");
  const report = realRecord(current.llm?.analyst_report);
  const fatigue = current.creative?.fatigue;
  const loi = current.loi || {};
  const leak = current.organic_leak || {};
  const context = realRecord(current.llm?.context);
  const pendingEvents = (context?.output?.events || []).filter((e) => !e.review).length;
  const planner = current.planner || {};
  return [
    { id: "watchdog", name: "Data Watchdog", kind: "CHECKS · FREE", status: agents.watchdog?.status,
      finding: `Last run ${run.watchdog_status || "—"}. Creative data: ${creativeWarn.length ? `${creativeWarn.length} check(s) to look at — ${creativeWarn.map((c) => problemOf(c).toLowerCase()).join("; ")}` : "all checks pass"}.`,
      meta: metaFor("measurement", "Measurement to"), go: { view: "creative" } },
    { id: "forensics", name: "Incident Investigator", kind: "EVIDENCE · FREE + LLM", status: agents.forensics?.status,
      finding: open.length ? `${open.length} open incident(s). ${hyps.length ? hyps.map((h) => `${h.incident_id.slice(0, 10)}: ${h.output.suggested_label.replaceAll("_", " ").toLowerCase()} (${h.output.confidence})`).join("; ") : "No hypothesis yet."}` : "No open incidents.",
      meta: { text: `${hyps.length} hypothesis(es) · ~$0.10 each`, stale: false }, go: { view: "incidents" } },
    { id: "analyst", name: "Weekly Briefing", kind: "LLM · ~$0.10", status: agents.analyst?.status,
      finding: report?.status === "COMPLETED" ? report.output.headline
        : `Not written yet. Its data is ready: ${fatigue?.flagged ?? 0} fatigued creative(s), ${Object.values(current.creative?.quality_vs_volume?.channels || {}).reduce((n, c) => n + (c.cheap_leads_weak_quality?.length || 0), 0)} cheap-but-weak creative(s), ${(loi.reasons?.digest?.movers || []).length} call-topic mover(s).`,
      meta: metaFor("creative", "Creative sheet"), go: { view: "creative" } },
    { id: "loi", name: "LOI Agent", kind: "CONSULTATIONS · FREE", status: agents.loi?.status,
      finding: loi.calibration ? `${loi.calibration.planner_outside_ci.length} Planner LOI rate(s) off; readiness ${loi.calibration.ai_order_holds ? "still signs in order" : "no longer signs in order"}. Hot share ${loi.weekly_quality?.last_vs_prior_4w?.hot_pct?.now ?? "—"}% last week.` : "Not computed yet.",
      meta: metaFor("loi", "Scores to"), go: { view: "loi" } },
    { id: "organic_leak", name: "Organic Leak Tracer", kind: "CLICK IDS · FREE", status: agents.organic_leak?.status,
      finding: leak.organic_labelled ? `${leak.traced_to_paid} of ${leak.organic_labelled} "organic" leads (${(100 * leak.traced_share).toFixed(1)}%) carry a paid click id: ${leak.by_platform?.meta ?? 0} Meta, ${leak.by_platform?.google ?? 0} Google. A floor, not a total.` : "No CRM export read yet.",
      meta: metaFor("crm_export", "CRM export to"), go: { node: "organic_leak" } },
    { id: "controls", name: "Context Scout", kind: "WEB · LLM ~$0.15", status: agents.context?.status,
      finding: context?.status === "COMPLETED" ? `${(context.output?.events || []).length} external event(s) proposed, ${pendingEvents} awaiting your review.` : "Not run yet. Searches for incentives, energy prices and holidays around unexplained days.",
      meta: { text: agents.context?.detail?.split(".")[0] || "", stale: false }, go: { node: "controls" } },
    { id: "planner", name: "Planner CatBoost", kind: "ALLOCATOR", status: agents.planner?.status,
      finding: `${planner.runtime?.backend ? "Backend up" : "Backend down"} at ${(planner.commit || "").slice(0, 8)}. ${planner.dirty_files?.length ? `${planner.dirty_files.length} local file(s) not committed.` : "Tree clean."} Budgets are decided there, by people.`,
      meta: { text: "Production writes from this app: off", stale: false }, go: { workspace: "planner" } },
    { id: "attribution", name: "Attribution pipeline", kind: "GATE → EV → DECISION", status: current.summary?.attribution_result,
      finding: "The evidence gate cannot pass on observational data by design; EV and Budget Decision stay locked until a real experiment (a platform lift test) lands.",
      meta: { text: "Blocked on: platform lead-conversion tracking fix", stale: true }, go: { node: "attribution" } },
  ];
}

function renderResults(current) {
  $("#agentResults").innerHTML = resultCards(current).map((card) => `
    <article class="result-card">
      <div class="rc-head"><div><p class="rc-kind">${escapeHtml(card.kind)}</p><h3>${escapeHtml(card.name)}</h3></div>
        <span class="agent-status ${statusClass(card.status)}"><i></i>${escapeHtml(card.status || "—")}</span></div>
      <p class="rc-finding">${escapeHtml(card.finding)}</p>
      <p class="rc-meta${card.meta.stale ? " stale" : ""}">${escapeHtml(card.meta.text)}</p>
      <button class="secondary-button" data-go-view="${escapeHtml(card.go.view || "")}" data-go-node="${escapeHtml(card.go.node || "")}" data-go-workspace="${escapeHtml(card.go.workspace || "")}">Open →</button>
    </article>`).join("");
}

function allocNote(note) {
  return `<li class="${note.tone === "warn" ? "warn" : ""}"><span>${escapeHtml(note.text)}</span><span class="src">${escapeHtml(note.source)}</span></li>`;
}

// The plan first, then what the agents say about it. Planner decides; this only reads.
function renderAllocation(current) {
  const a = current.allocation || {};
  const badge = $("#allocationBadge");
  if (a.status !== "READY") {
    $("#allocationTitle").textContent = "Latest plan";
    badge.textContent = a.status || "—";
    badge.className = "badge muted";
    $("#allocationContent").innerHTML = `<p class="panel-note">${escapeHtml(a.detail || "No allocation available.")}</p>`;
    return;
  }
  $("#allocationTitle").textContent = `Planner plan #${a.run_id} for ${a.valid_for} · ${a.metodo || "classic"}`;
  badge.textContent = a.economics_real ? String(a.run_status).toUpperCase() : `${String(a.run_status).toUpperCase()} · TEST DATA`;
  badge.className = `badge ${a.economics_real ? (a.run_status === "approved" ? "ready" : "danger") : "danger"}`;
  const kpis = [["BUDGET", fmtEur(a.budget_eur)], ["EXPECTED LEADS", fmtNum(a.leads)], ["AVERAGE CPL", fmtEur(a.cpl_eur)], ["ACTIVE CELLS", fmtNum(a.active_cells)]]
    .map(([label, value]) => `<div><span>${label}</span><strong>${escapeHtml(value)}</strong></div>`).join("");
  const rows = (a.channels || []).map((c) => ({ ...c, comments_html: (c.comments || []).map(allocNote).join("") }));
  const head = [["Channel", "left"], ["Budget"], ["Share"], ["Share of spend last week"], ["Planner CPL"], ["Actual CPL last week"], ["What the agents say", "left"]]
    .map(([label, align]) => `<th${align ? ' style="text-align:left"' : ""}>${escapeHtml(label)}</th>`).join("");
  // data-label feeds the stacked layout on narrow screens (CSS shows it as the cell's caption).
  const body = rows.map((c) => `<tr>
      <td data-label="Channel">${escapeHtml(c.channel)}</td><td data-label="Budget">${fmtEur(c.budget_eur)}</td><td data-label="Share">${fmtPct(c.share_pct, 0)}</td>
      <td data-label="Spend share last week">${c.recent_spend_share_pct == null ? "—" : fmtPct(c.recent_spend_share_pct, 0)}</td>
      <td data-label="Planner CPL">${fmtEur(c.cpl_model_eur)}</td>
      <td data-label="Actual CPL last week" class="${c.recent_cpl_change_pct > 0 ? "up" : c.recent_cpl_change_pct < 0 ? "down" : ""}">${fmtEur(c.recent_cpl_eur)}${c.recent_cpl_change_pct == null ? "" : ` (${fmtDelta(c.recent_cpl_change_pct)})`}</td>
      <td class="comments"><ul class="alloc-notes">${c.comments_html || '<li><span class="panel-note">Nothing to add.</span></li>'}</ul></td>
    </tr>`).join("");
  $("#allocationContent").innerHTML = `
    <div class="alloc-kpis">${kpis}</div>
    <p class="report-label">WHAT THE AGENTS SAY ABOUT THIS PLAN</p>
    <ul class="alloc-notes">${(a.plan_comments || []).map(allocNote).join("")}</ul>
    <p class="report-label">PER CHANNEL</p>
    <div class="table-wrap"><table class="data-table alloc-table"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>
    ${pacingTable(current.pacing)}
    <div class="alloc-actions"><button class="secondary-button" data-go-workspace="planner">Open in Planner →</button>
      <span class="panel-note">Approving, rejecting and exporting happen in Planner. Comments describe; the budget is a human decision.</span></div>`;
}

const VERDICT_TEXT = { under_delivering: ["Not spending its budget", "warn"], budget_limited: ["Budget-limited", "info"],
  budget_changed: ["Budget changed since", "muted"], on_track: ["On track", "ok"] };

// Budgets as set in Meta/Google vs last week's spend. A delivery check, not the planner's plan.
function pacingTable(pacing) {
  if (!pacing?.channels?.length) return "";
  const rows = pacing.channels.map((c) => `<tr><td>${escapeHtml(c.channel)}</td><td>${fmtEur(c.daily_budget_eur)}</td>
      <td>${fmtEur(c.week_spend_eur)}</td><td class="${c.utilisation_pct < 60 ? "up" : ""}">${fmtPct(c.utilisation_pct, 0)}</td>
      <td>${c.under_delivering}</td><td>${c.budget_limited}</td></tr>`).join("");
  const flagged = (pacing.flagged || []).slice(0, 8).map((c) => {
    const [text, tone] = VERDICT_TEXT[c.verdict] || [c.verdict, "muted"];
    return `<li><span class="tag ${tone}">${escapeHtml(text)}</span> <strong>${escapeHtml(c.campaign)}</strong> (${escapeHtml(c.channel)}): ${fmtPct(c.utilisation_pct, 0)} of ${fmtEur(c.daily_budget_eur)}/day${c.google_says_limited ? " · Google says limited by budget" : ""}</li>`;
  }).join("");
  return `<p class="report-label">BUDGETS SET IN THE PLATFORMS · WEEK OF ${escapeHtml(pacing.week)}</p>
    <div class="table-wrap"><table class="data-table"><thead><tr><th>Channel</th><th>Budgets set / day</th><th>Spent last week</th><th>Share spent</th><th>Not spending</th><th>Budget-limited</th></tr></thead><tbody>${rows}</tbody></table></div>
    ${flagged ? `<ul class="pacing-flags">${flagged}</ul>` : ""}
    <p class="panel-note">${escapeHtml(pacing.note || "")}</p>`;
}

function renderRunSummary(current) {
  const last = current.last_run || {};
  const box = $("#runSummary");
  if (!last.finished_at_utc || sessionStorageGet("dismissedRun") === last.finished_at_utc) { box.hidden = true; return; }
  const warn = attentionItems(current).filter((i) => i.tone === "warn").length;
  box.hidden = false;
  box.innerHTML = `
    <button class="icon-button rs-close" data-dismiss-run="${escapeHtml(last.finished_at_utc)}" aria-label="Dismiss">×</button>
    <h3>Last run ${escapeHtml(new Date(last.finished_at_utc).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }))}: ${warn ? `${warn} thing(s) to look at` : "nothing needs a look"}</h3>
    <ul>
      <li>Data Watchdog: ${escapeHtml(last.run_status || "—")} (measurement table to ${escapeHtml(last.analysis_as_of || "—")})</li>
      <li>Creative checks: ${escapeHtml(last.creative_status || "not run")} · LOI Agent: ${escapeHtml(last.loi_status || "not run")} · Budget pacing: ${escapeHtml(last.pacing_status || "not run")}</li>
      <li>Results are in the boxes below; the attention list says what to open first.</li>
    </ul>`;
}

function sessionStorageGet(key) { try { return window.sessionStorage.getItem(key); } catch { return null; } }
function sessionStorageSet(key, value) { try { window.sessionStorage.setItem(key, value); } catch { /* private mode */ } }

document.addEventListener("click", (event) => {
  const dismiss = event.target.closest("[data-dismiss-run]");
  if (dismiss) { sessionStorageSet("dismissedRun", dismiss.dataset.dismissRun); $("#runSummary").hidden = true; return; }
  const go = event.target.closest("[data-go-view], [data-go-node], [data-go-workspace]");
  if (!go || !(go.dataset.goView || go.dataset.goNode || go.dataset.goWorkspace)) return;
  if (go.dataset.goWorkspace) { switchWorkspace(go.dataset.goWorkspace); return; }
  if (go.dataset.goNode) { selectedNodeId = go.dataset.goNode; showAttributionView("agents"); if (state) renderNetworks(state); return; }
  showAttributionView(go.dataset.goView);
  window.scrollTo({ top: 0 });
});

function render(next) {
  state = next;
  const weeks = next.summary.weekly_observations ?? 0;
  renderFreshness(next);
  $("#weeklyObservations").textContent = weeks;
  $("#coverageNote").textContent = next.extended_history?.usable_complete_weeks ? `${next.summary.current_weekly_observations} current + ${next.extended_history.legacy_weeks} legacy weeks` : "weekly observations";
  $("#openIncidents").textContent = next.summary.open_incidents;
  $("#attributionGate").textContent = next.summary.attribution_result.replaceAll("_", " ");
  $("#attributionGate").className = { blocked: "red", muted: "", danger: "amber" }[verdictTone(next.summary.attribution_result)];
  $("#plannerStatus").textContent = next.planner?.available ? "CONNECTED" : "OFFLINE";
  $("#plannerStatus").className = next.planner?.available ? "mint" : "amber";
  $("#plannerMeta").textContent = next.planner?.commit ? `v6 · ${next.planner.commit.slice(0, 8)} · production locked` : "CatBoost production safe";
  $("#monitoringEnabled").checked = next.control.monitoring_enabled;
  $("#scheduleMode").value = next.control.schedule_mode;
  $("#dailyTime").value = next.control.daily_time;
  $("#monitoringBadge").textContent = next.control.monitoring_enabled ? "ENABLED" : "PAUSED";
  $("#monitoringBadge").className = `badge ${next.control.monitoring_enabled ? "ready" : "muted"}`;
  renderHistory(next);
  $("#readinessBadge").textContent = next.summary.attribution_result.replaceAll("_", " ");
  $("#readinessBadge").className = `badge ${verdictTone(next.summary.attribution_result)}`;
  $("#evidenceInterpretation").textContent = next.classification.interpretation || "Attribution has not been classified yet.";
  const historyAudit = next.history_audit || {};
  const extended = next.extended_history || {};
  $("#historyAuditDecision").textContent = extended.decision
    ? `${extended.decision.replaceAll("_", " ")} · ${extended.usable_complete_weeks} complete weeks`
    : (historyAudit.decision ? historyAudit.decision.replaceAll("_", " ") : "Legacy history has not been audited yet.");
  renderNetworks(next);
  renderRuns(next.runs || []);
  renderIncidents(next.incidents || [], next.llm || {});
  renderPolicy(next.policy || {});
  renderPlannerRuntime(next);
  renderCreative(next);
  renderLoi(next);
  renderAllocation(next);
  renderAttention(next);
  renderResults(next);
  renderRunSummary(next);
}

async function loadState() {
  try { render(await api("/api/state")); }
  catch (error) { toast(error.message, true); }
}

function showAttributionView(viewName) {
  activeAttributionView = viewName;
  if (!window.location.hash.startsWith(`#/${viewName}`)) history.replaceState(null, "", `#/${viewName}`);
  $$('.nav-item').forEach((item) => item.classList.remove("active"));
  $$('.view').forEach((view) => view.classList.remove("active"));
  const button = $(`[data-view="${viewName}"]`);
  button.classList.add("active");
  $(`#${viewName}`).classList.add("active");
  $("#viewTitle").textContent = ({ overview: "Attribution model + agents", agents: "Agent network", incidents: "Incident review", policy: "Schedule + policy", creative: "Creative performance", loi: "LOI agent" })[viewName];
}

function switchWorkspace(workspace) {
  activeWorkspace = workspace;
  $$('.workspace-button').forEach((item) => item.classList.toggle("active", item.dataset.workspace === workspace));
  $("#attributionNav").hidden = workspace !== "attribution";
  $(".nav-label").hidden = workspace !== "attribution";
  $("#runButton").hidden = workspace !== "attribution";
  $$('.view').forEach((view) => view.classList.remove("active"));
  if (workspace === "planner") {
    $("#plannerWorkspace").classList.add("active");
    $("#viewTitle").textContent = "Planner allocator";
    if (state) renderPlannerRuntime(state);
  } else {
    showAttributionView(activeAttributionView);
  }
}

$$('.nav-item').forEach((button) => button.addEventListener("click", () => showAttributionView(button.dataset.view)));
$$('.workspace-button').forEach((button) => button.addEventListener("click", () => switchWorkspace(button.dataset.workspace)));
$("#retryPlanner").addEventListener("click", loadState);

$("#refreshButton").addEventListener("click", loadState);
$("#runButton").addEventListener("click", async () => {
  const button = $("#runButton");
  button.disabled = true;
  button.textContent = "Running: checks, creative sheet, LOI (1–2 min)…";
  try {
    const result = await api("/api/run", { method: "POST", body: "{}" });
    sessionStorageSet("dismissedRun", "");
    await loadState();
    switchWorkspace("attribution");
    showAttributionView("overview");
    window.scrollTo({ top: 0, behavior: "smooth" });
    toast(`Run finished: watchdog ${result.run_status}, creative ${result.creative_status}, LOI ${result.loi_status}.`);
  } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; button.innerHTML = '<span class="pulse"></span> Run agents now'; }
});

$("#creativeRefresh").addEventListener("click", async () => {
  const button = $("#creativeRefresh");
  button.disabled = true;
  button.textContent = "Fetching sheet…";
  try {
    const result = await api("/api/creative/refresh", { method: "POST", body: JSON.stringify({ fetch: true }) });
    toast(`Creative data refreshed: checks ${result.watchdog_status}.`);
    await loadState();
  } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; button.textContent = "Fetch sheet + recompute"; }
});

$("#loiRefresh").addEventListener("click", async () => {
  const button = $("#loiRefresh");
  button.disabled = true;
  try {
    const result = await api("/api/loi/refresh", { method: "POST", body: "{}" });
    toast(`LOI feed recomputed from ${result.file}: ${result.loi_status}.`);
    await loadState();
  } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; }
});

$("#creativeBriefing").addEventListener("click", async () => {
  $("#creativeBriefing").disabled = true;
  try {
    await api("/api/llm/run", { method: "POST", body: JSON.stringify({ agent: "analyst" }) });
    toast("Writing the weekly briefing…");
    await waitForLlm();
    toast("Briefing written.");
  } catch (error) { toast(error.message, true); }
  await loadState();
});

$("#saveControl").addEventListener("click", async () => {
  try {
    await api("/api/control", {
      method: "POST",
      body: JSON.stringify({
        monitoring_enabled: $("#monitoringEnabled").checked,
        schedule_mode: $("#scheduleMode").value,
        daily_time: $("#dailyTime").value,
      }),
    });
    toast("Monitoring control saved locally.");
    await loadState();
  } catch (error) { toast(error.message, true); }
});

$("#policyForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const payload = Object.fromEntries(new FormData(event.currentTarget));
  try {
    await api("/api/policy", { method: "POST", body: JSON.stringify(payload) });
    $("#policyStatus").textContent = "Saved locally";
    toast("Monitoring policy saved.");
    await loadState();
  } catch (error) { $("#policyStatus").textContent = error.message; toast(error.message, true); }
});

// A link like /#/overview or /#/loi opens that page directly (bookmarkable). The "/"
// keeps the browser from also jumping to the element whose id is the view name.
const VIEWS = ["agents", "overview", "incidents", "policy", "creative", "loi"];
function openFromHash() {
  // "#/agents/loi" also selects that node in the Network graph.
  const [view, node] = window.location.hash.replace(/^#\/?/, "").split("/");
  if (node) selectedNodeId = node;
  if (VIEWS.includes(view)) showAttributionView(view);
  if (node && state) renderNetworks(state);
}
window.addEventListener("hashchange", openFromHash);
openFromHash();
loadState();

function registerWebMcpTools() {
  const context = document.modelContext;
  if (!context?.registerTool) return;
  const tools = [
    {
      name: "read_attribution_control_status",
      title: "Read attribution status",
      description: "Read the same current measurement, agent, incident and evidence-gate status shown in the control center.",
      inputSchema: { type: "object", properties: {}, additionalProperties: false },
      annotations: { readOnlyHint: true, untrustedContentHint: false },
      async execute() {
        const current = await api("/api/state");
        render(current);
        return { summary: current.summary, measurement: current.measurement, extended_history: current.extended_history, planner: current.planner };
      },
    },
    {
      name: "run_local_monitoring_cycle",
      title: "Run local monitoring cycle",
      description: "Run the existing watchdog, incident synchronization and local forensic checks once. Does not refresh data or change production allocation.",
      inputSchema: { type: "object", properties: {}, additionalProperties: false },
      annotations: { readOnlyHint: false, untrustedContentHint: false },
      async execute() {
        const result = await api("/api/run", { method: "POST", body: "{}" });
        await loadState();
        return result;
      },
    },
    {
      name: "configure_local_monitoring_schedule",
      title: "Configure local monitoring",
      description: "Enable, pause or schedule local monitoring while this control-center server is running.",
      inputSchema: {
        type: "object",
        properties: {
          monitoring_enabled: { type: "boolean" },
          schedule_mode: { type: "string", enum: ["manual", "daily"] },
          daily_time: { type: "string", pattern: "^(?:[01]\\d|2[0-3]):[0-5]\\d$" },
        },
        required: ["monitoring_enabled", "schedule_mode", "daily_time"],
        additionalProperties: false,
      },
      annotations: { readOnlyHint: false, untrustedContentHint: false },
      async execute(input) {
        const result = await api("/api/control", { method: "POST", body: JSON.stringify(input) });
        await loadState();
        return result;
      },
    },
  ];
  tools.forEach((tool) => {
    try { void Promise.resolve(context.registerTool(tool)).catch(() => {}); } catch (_) { /* unsupported draft implementation */ }
  });
}

registerWebMcpTools();

let resizeTimer;
window.addEventListener("resize", () => {
  window.clearTimeout(resizeTimer);
  resizeTimer = window.setTimeout(() => { if (state) renderNetworks(state); }, 140);
});
