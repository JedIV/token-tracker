// token-tracker UI

const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));

const fmt = {
  n: (x) => (x == null ? "—" : Math.round(x).toLocaleString()),
  // compact form for axis labels
  short: (x) => {
    if (x == null) return "—";
    const abs = Math.abs(x);
    if (abs >= 1e9) return (x / 1e9).toFixed(2) + "B";
    if (abs >= 1e6) return (x / 1e6).toFixed(2) + "M";
    if (abs >= 1e3) return (x / 1e3).toFixed(1) + "K";
    return String(x);
  },
  usd: (x) => x == null ? "—" : "$" + Number(x).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }),
  bytes: (x) => {
    if (x == null) return "—";
    const units = ["B", "KB", "MB", "GB"];
    let i = 0; let n = x;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return n.toFixed(i ? 1 : 0) + " " + units[i];
  },
  date: (s) => s ? s.replace("T", " ").replace("Z", "").slice(0, 19) : "—",
  pathTail: (p, n = 38) => {
    if (!p) return "—";
    return p.length > n ? "…" + p.slice(-n + 1) : p;
  },
};

const MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

function bucketParts(bucket, granularity) {
  // Returns a Date (always in UTC). The bucket string is UTC.
  if (granularity === "week") {
    const m = bucket.match(/^(\d{4})-W(\d{1,2})$/);
    if (!m) return new Date(NaN);
    // ISO-ish week: take Jan 1 + (week-1)*7 days. SQLite %W is "week of year, Mon as first day",
    // which doesn't exactly match ISO but is close enough for a label.
    const jan1 = new Date(Date.UTC(Number(m[1]), 0, 1));
    return new Date(jan1.getTime() + (Number(m[2]) - 1) * 7 * 86400 * 1000);
  }
  if (granularity === "month") return new Date(bucket + "-01T00:00:00Z");
  if (granularity === "day") return new Date(bucket + "T00:00:00Z");
  if (granularity === "hour") return new Date(bucket + ":00:00Z");
  if (granularity === "minute") return new Date(bucket + ":00Z");
  return new Date(bucket);
}

function bucketLabel(bucket, granularity) {
  const d = bucketParts(bucket, granularity);
  if (isNaN(d.getTime())) return bucket;
  // Use UTC components to avoid timezone shifting from buckets in DB.
  const mo = MONTH_NAMES[d.getUTCMonth()];
  const dd = d.getUTCDate();
  const hh = String(d.getUTCHours()).padStart(2, "0");
  const mm = String(d.getUTCMinutes()).padStart(2, "0");
  switch (granularity) {
    case "minute": return `${mo} ${dd} ${hh}:${mm}`;
    case "hour":   return `${mo} ${dd} ${hh}:00`;
    case "day":    return `${mo} ${dd}`;
    case "week":   return `wk of ${mo} ${dd}`;
    case "month":  return `${mo} ${d.getUTCFullYear()}`;
    default:       return bucket;
  }
}

function bucketTooltipTitle(bucket, granularity) {
  const d = bucketParts(bucket, granularity);
  if (isNaN(d.getTime())) return bucket;
  const iso = d.toISOString();
  switch (granularity) {
    case "minute": return iso.slice(0, 16).replace("T", " ") + " UTC";
    case "hour":   return iso.slice(0, 13).replace("T", " ") + ":00 UTC";
    case "day":    return iso.slice(0, 10);
    case "week":   return `week starting ${iso.slice(0, 10)}`;
    case "month":  return iso.slice(0, 7);
    default:       return bucket;
  }
}

const STATE = { filters: { tool: "", model: "", project: "", start: "", end: "", granularity: "auto" } };
let usageChart, costChart, breakdownChart;

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error(`${path}: ${r.status}`);
  return r.json();
}

function queryString(extra = {}) {
  const p = { ...STATE.filters, ...extra };
  const u = new URLSearchParams();
  for (const k of Object.keys(p)) if (p[k]) u.set(k, p[k]);
  const q = u.toString();
  return q ? "?" + q : "";
}

function fillSelect(el, values, current = "") {
  el.innerHTML = '<option value="">all</option>';
  for (const v of values) {
    const o = document.createElement("option");
    o.value = v; o.textContent = v;
    if (v === current) o.selected = true;
    el.appendChild(o);
  }
}

async function loadFilters() {
  const d = await api("/api/filters");
  fillSelect($("#f-tool"), d.tools, STATE.filters.tool);
  fillSelect($("#f-model"), d.models, STATE.filters.model);
  fillSelect($("#f-project"), d.projects, STATE.filters.project);
  if (d.date_range.min) {
    $("#f-start").min = d.date_range.min.slice(0, 10);
    $("#f-end").max = d.date_range.max.slice(0, 10);
  }
}

function renderCards(totals) {
  const tokens_in = totals.input_tokens || 0;
  const tokens_out = totals.output_tokens || 0;
  const tokens_hit = totals.cache_hit || 0;
  const tokens_cw5 = totals.cache_write_5m || 0;
  const tokens_cw1 = totals.cache_write_1h || 0;
  const cost = totals.cost_usd || 0;
  const ah = totals.active_hours || 0;
  const cph = totals.cost_per_hour || 0;
  const freshTokens = tokens_in + tokens_out + tokens_cw5 + tokens_cw1;
  const allTokens = freshTokens + tokens_hit;
  const cacheShare = allTokens ? tokens_hit / allTokens : 0;
  const cards = [
    { label: "est cost", value: fmt.usd(cost), accent: true },
    { label: "$ / active hour", value: fmt.usd(cph), accent: true, sub: "Σ session spans" },
    { label: "fresh + output tokens", value: fmt.short(freshTokens), sub: fmt.n(freshTokens) },
    { label: "cache share", value: Math.round(cacheShare * 100) + "%", sub: fmt.n(tokens_hit) + " cache hits" },
    { label: "sessions", value: fmt.n(totals.sessions) },
    { label: "messages", value: fmt.n(totals.msgs) },
    { label: "active hours", value: ah < 1 ? ah.toFixed(2) : ah.toFixed(1) },
    { label: "cache writes", value: fmt.short(tokens_cw5 + tokens_cw1), sub: fmt.n(tokens_cw5 + tokens_cw1) },
  ];
  $("#cards").innerHTML = cards.map(c => `
    <div class="card ${c.accent ? "accent" : ""}">
      <div class="label">${c.label}</div>
      <div class="value">${c.value}</div>
      ${c.sub ? `<div class="sub">${c.sub}</div>` : ""}
    </div>`).join("");
}

const chartFontColor = "#9aa1ad";
const chartGridColor = "#2a2e36";
const chartCommon = {
  responsive: true,
  maintainAspectRatio: false,
  interaction: { mode: "index", intersect: false },
  plugins: {
    legend: { labels: { color: chartFontColor, font: { family: "ui-monospace, monospace", size: 11 } } },
    tooltip: {
      backgroundColor: "#16181d",
      borderColor: "#3a3f49",
      borderWidth: 1,
      titleColor: "#e6e8ec",
      bodyColor: "#e6e8ec",
    },
  },
  scales: {
    x: { ticks: { color: chartFontColor, font: { family: "ui-monospace, monospace", size: 10 } }, grid: { color: chartGridColor } },
    y: { ticks: { color: chartFontColor, font: { family: "ui-monospace, monospace", size: 10 }, callback: (v) => fmt.short(v) }, grid: { color: chartGridColor } },
  },
};

function renderCharts(daily, granularity) {
  if (typeof Chart === "undefined") {
    $("#gran-label").textContent = "· charts unavailable";
    return;
  }
  const buckets = daily.map(d => d.bucket);
  const labels = buckets.map(b => bucketLabel(b, granularity));
  $("#gran-label").textContent = `· bucket: ${granularity} · ${daily.length} points`;

  // Auto-rotate labels and limit visible tick count so wide ranges aren't unreadable.
  const xTicks = {
    color: chartFontColor,
    font: { family: "ui-monospace, monospace", size: 10 },
    autoSkip: true,
    maxRotation: 0,
    minRotation: 0,
    maxTicksLimit: Math.min(12, labels.length),
  };
  const sharedTooltip = {
    ...chartCommon.plugins.tooltip,
    callbacks: { title: (items) => bucketTooltipTitle(buckets[items[0].dataIndex], granularity) },
  };

  const colors = {
    in: "#7cc4ff", out: "#ffd479", hit: "#7fe6a8", cw5: "#c9a4ff", cw1: "#ff9bb3", usd: "#ffd479",
  };
  const titleStyle = (text) => ({ display: true, text, color: "#9aa1ad", font: { size: 11, family: "ui-monospace, monospace" } });
  try {
    if (usageChart) usageChart.destroy();
    usageChart = new Chart($("#chart-usage"), {
      type: "bar",
      data: {
        labels,
        datasets: [
          {
            label: "input",
            data: daily.map(d => d.input_tokens),
            backgroundColor: colors.in,
            borderColor: colors.in,
            stack: "tokens",
          },
          {
            label: "output",
            data: daily.map(d => d.output_tokens),
            backgroundColor: colors.out,
            borderColor: colors.out,
            stack: "tokens",
          },
          {
            label: "cache writes",
            data: daily.map(d => (d.cache_write_5m || 0) + (d.cache_write_1h || 0)),
            backgroundColor: colors.cw5,
            borderColor: colors.cw5,
            stack: "tokens",
          },
        ],
      },
      options: {
        ...chartCommon,
        plugins: { ...chartCommon.plugins, title: titleStyle("non-cache tokens by bucket"), tooltip: sharedTooltip },
        scales: {
          x: { ...chartCommon.scales.x, ticks: xTicks, stacked: true },
          y: { ...chartCommon.scales.y, stacked: true },
        },
      },
    });

    if (costChart) costChart.destroy();
    costChart = new Chart($("#chart-cost"), {
      type: "bar",
      data: {
        labels,
        datasets: [{
          label: "cost",
          data: daily.map(d => d.cost_usd),
          backgroundColor: "rgba(255, 212, 121, 0.72)",
          borderColor: colors.usd,
        }],
      },
      options: {
        ...chartCommon,
        plugins: { ...chartCommon.plugins, title: titleStyle("estimated cost by bucket"), tooltip: sharedTooltip },
        scales: {
          x: { ...chartCommon.scales.x, ticks: xTicks },
          y: { ...chartCommon.scales.y, ticks: { ...chartCommon.scales.y.ticks, callback: (v) => "$" + fmt.short(v) } },
        },
      },
    });
  } catch (e) {
    console.error("main chart failed", e);
    $("#gran-label").textContent = `· bucket: ${granularity} · chart failed`;
  }
}

function labelForGroupRow(group, r) {
  if (group === "tool") return r.tool;
  if (group === "model") return r.model;
  if (group === "project") return fmt.pathTail(r.project, 32);
  if (group === "session") return `${r.tool} · ${fmt.pathTail(r.cwd || r.id, 28)}`;
  if (group === "server") return r.server;
  if (group === "mcp_tool") return `${r.server} · ${r.tool_name}`;
  return "";
}

async function renderBreakdownChart(group) {
  if (typeof Chart === "undefined") return;
  if (breakdownChart) breakdownChart.destroy();
  const canvas = $("#chart-breakdown");
  try {
    const d = await api("/api/breakdown_series" + queryString({ group, limit: 5 }));
    if (!d.series || !d.series.length) {
      breakdownChart = null;
      return;
    }

    const labels = d.buckets.map(b => bucketLabel(b, d.granularity));
    const buckets = d.buckets;
    const palette = ["#ffd479", "#7cc4ff", "#7fe6a8", "#ff9bb3", "#c9a4ff"];
    breakdownChart = new Chart(canvas, {
      type: "line",
      data: {
        labels,
        datasets: d.series.map((s, i) => ({
          label: fmt.pathTail(s.label, 58),
          data: s.points.map(p => p.cost_usd),
          borderColor: palette[i % palette.length],
          backgroundColor: palette[i % palette.length],
          tension: 0.25,
          pointRadius: 2,
          fill: false,
        })),
      },
      options: {
        ...chartCommon,
        plugins: {
          ...chartCommon.plugins,
          title: { display: true, text: "top 5 by estimated cost over time", color: "#9aa1ad", font: { size: 11, family: "ui-monospace, monospace" } },
          tooltip: {
            ...chartCommon.plugins.tooltip,
            callbacks: { title: (items) => bucketTooltipTitle(buckets[items[0].dataIndex], d.granularity) },
          },
        },
        scales: {
          x: { ...chartCommon.scales.x, ticks: { ...chartCommon.scales.x.ticks, autoSkip: true, maxRotation: 0, minRotation: 0, maxTicksLimit: Math.min(12, labels.length) } },
          y: { ...chartCommon.scales.y, ticks: { ...chartCommon.scales.y.ticks, callback: (v) => "$" + fmt.short(v) } },
        },
      },
    });
  } catch (e) {
    console.error("breakdown chart failed", e);
    breakdownChart = null;
  }
}

function tableHTML(cols, rows, opts = {}) {
  if (!rows || !rows.length) return `<div class="empty">no data</div>`;
  const head = `<thead><tr>${cols.map(c => `<th class="${c.num ? "num" : ""}">${c.label}</th>`).join("")}</tr></thead>`;
  const body = `<tbody>${rows.map(r => `<tr${opts.click ? ` data-row='${JSON.stringify(r).replace(/'/g, "&#39;")}' class="clickable"` : ""}>${cols.map(c => {
    let v = c.fmt ? c.fmt(r[c.key], r) : r[c.key];
    if (v == null) v = "—";
    return `<td class="${c.num ? "num" : ""}${c.css ? " " + c.css : ""}">${v}</td>`;
  }).join("")}</tr>`).join("")}</tbody>`;
  return `<table>${head}${body}</table>`;
}

function renderTable(sel, html) { $(sel).outerHTML = `<table id="${sel.slice(1)}">${html === "" ? "" : html.replace(/^<table>|<\/table>$/g, "")}</table>`; }

function setTable(sel, cols, rows, opts = {}) {
  const t = $(sel);
  if (!rows || !rows.length) {
    t.innerHTML = `<tbody><tr><td class="dim">no data</td></tr></tbody>`;
    return;
  }
  const head = `<thead><tr>${cols.map(c => `<th class="${c.num ? "num" : ""}">${c.label}</th>`).join("")}</tr></thead>`;
  const body = rows.map((r, i) => {
    const tds = cols.map(c => {
      let v = c.fmt ? c.fmt(r[c.key], r) : r[c.key];
      if (v == null) v = "—";
      return `<td class="${c.num ? "num" : ""}${c.css ? " " + c.css : ""}">${v}</td>`;
    }).join("");
    return `<tr${opts.click ? ` data-idx="${i}" class="clickable"` : ""}>${tds}</tr>`;
  }).join("");
  t.innerHTML = head + `<tbody>${body}</tbody>`;
  if (opts.click) {
    t.querySelectorAll("tr.clickable").forEach(tr => {
      tr.addEventListener("click", () => opts.click(rows[Number(tr.dataset.idx)]));
    });
  }
}

async function loadStats() {
  const d = await api("/api/stats" + queryString());
  renderCards(d.totals);
  renderCharts(d.daily, d.granularity);
  STATE.statsCache = d;
  await loadBreakdown();
}

// Cache MCP data so toggling between server / mcp_tool is instant.
async function getMcpData() {
  if (STATE.mcpCache && STATE.mcpCacheKey === queryString()) return STATE.mcpCache;
  const d = await api("/api/mcp" + queryString());
  STATE.mcpCache = d;
  STATE.mcpCacheKey = queryString();
  return d;
}

const BREAKDOWN_COLS = {
  tool: [
    { key: "tool", label: "tool" },
    { key: "msgs", label: "msgs", num: true, fmt: fmt.n },
    { key: "input_tokens", label: "input", num: true, fmt: fmt.n },
    { key: "output_tokens", label: "output", num: true, fmt: fmt.n },
    { key: "cache_hit", label: "cache hits", num: true, fmt: fmt.n },
    { key: "cache_write_5m", label: "write 5m", num: true, fmt: fmt.n },
    { key: "cache_write_1h", label: "write 1h", num: true, fmt: fmt.n },
    { key: "cost_usd", label: "cost", num: true, fmt: fmt.usd, css: "cost" },
  ],
  model: [
    { key: "model", label: "model" },
    { key: "tool", label: "tool", css: "dim" },
    { key: "msgs", label: "msgs", num: true, fmt: fmt.n },
    { key: "input_tokens", label: "input", num: true, fmt: fmt.n },
    { key: "output_tokens", label: "output", num: true, fmt: fmt.n },
    { key: "cache_hit", label: "cache hits", num: true, fmt: fmt.n },
    { key: "cache_write_5m", label: "write 5m", num: true, fmt: fmt.n },
    { key: "cache_write_1h", label: "write 1h", num: true, fmt: fmt.n },
    { key: "cost_usd", label: "cost", num: true, fmt: fmt.usd, css: "cost" },
  ],
  project: [
    { key: "project", label: "project", fmt: (v) => fmt.pathTail(v, 60) },
    { key: "sessions", label: "sessions", num: true, fmt: fmt.n },
    { key: "msgs", label: "msgs", num: true, fmt: fmt.n },
    { key: "input_tokens", label: "input", num: true, fmt: fmt.n },
    { key: "output_tokens", label: "output", num: true, fmt: fmt.n },
    { key: "cache_hit", label: "cache hits", num: true, fmt: fmt.n },
    { key: "cache_write_5m", label: "write 5m", num: true, fmt: fmt.n },
    { key: "cache_write_1h", label: "write 1h", num: true, fmt: fmt.n },
    { key: "cost_usd", label: "cost", num: true, fmt: fmt.usd, css: "cost" },
  ],
  session: [
    { key: "tool", label: "tool" },
    { key: "model", label: "model", css: "dim" },
    { key: "cwd", label: "project", fmt: (v) => fmt.pathTail(v, 50) },
    { key: "msg_count", label: "msgs", num: true, fmt: fmt.n },
    { key: "input_tokens", label: "input", num: true, fmt: fmt.n },
    { key: "output_tokens", label: "output", num: true, fmt: fmt.n },
    { key: "cache_read", label: "cache hits", num: true, fmt: fmt.n },
    { key: "cache_write", label: "cache write", num: true, fmt: fmt.n },
    { key: "started_at", label: "started", fmt: fmt.date, css: "dim" },
    { key: "est_cost_usd", label: "cost", num: true, fmt: fmt.usd, css: "cost" },
  ],
  server: [
    { key: "server", label: "mcp server" },
    { key: "calls", label: "calls", num: true, fmt: fmt.n },
    { key: "sessions", label: "sessions", num: true, fmt: fmt.n },
    { key: "result_chars", label: "result bytes", num: true, fmt: fmt.bytes },
    { key: "est_tokens", label: "est tokens", num: true, fmt: fmt.n },
    { key: "avg_lifetime_reads", label: "avg reads/call", num: true, fmt: (v) => v.toFixed(1) },
    { key: "errors", label: "errors", num: true, fmt: fmt.n, css: "dim" },
    { key: "est_cost_usd", label: "est cost", num: true, fmt: fmt.usd, css: "cost" },
    { key: "session_cost_usd", label: "session $ total", num: true, fmt: fmt.usd, css: "dim" },
  ],
  mcp_tool: [
    { key: "server", label: "mcp server" },
    { key: "tool_name", label: "tool" },
    { key: "calls", label: "calls", num: true, fmt: fmt.n },
    { key: "result_chars", label: "result bytes", num: true, fmt: fmt.bytes },
    { key: "est_tokens", label: "est tokens", num: true, fmt: fmt.n },
    { key: "avg_lifetime_reads", label: "avg reads/call", num: true, fmt: (v) => v.toFixed(1) },
    { key: "errors", label: "errors", num: true, fmt: fmt.n, css: "dim" },
    { key: "est_cost_usd", label: "est cost", num: true, fmt: fmt.usd, css: "cost" },
  ],
};

async function loadBreakdown() {
  const g = STATE.groupBy || "model";
  const ctrls = $("#group-controls");
  const hint = $("#group-hint");
  let rows = [];
  let clickFn = null;

  if (g === "tool" || g === "model" || g === "project") {
    const d = STATE.statsCache || await api("/api/stats" + queryString());
    rows = g === "tool" ? d.by_tool : g === "model" ? d.by_model : d.by_project;
    ctrls.innerHTML = "";
    hint.textContent = "";
  } else if (g === "session") {
    const sort = STATE.sessSort || "cost";
    const d = await api("/api/sessions" + queryString({ sort, limit: 200 }));
    rows = d.sessions;
    ctrls.innerHTML = `<label>sort
      <select id="sess-sort">
        <option value="cost">cost</option>
        <option value="recent">recent</option>
        <option value="messages">messages</option>
      </select>
    </label>`;
    $("#sess-sort").value = sort;
    $("#sess-sort").addEventListener("change", () => { STATE.sessSort = $("#sess-sort").value; loadBreakdown(); });
    hint.textContent = "click a row for the session timeline";
    clickFn = (r) => showSession(r.id);
  } else if (g === "server") {
    const d = await getMcpData();
    rows = d.by_server;
    ctrls.innerHTML = "";
    hint.textContent = "est cost = tokens × (cache-write once + cache-read × subsequent turns)";
    clickFn = (r) => showMcpServer(r.server);
  } else if (g === "mcp_tool") {
    const d = await getMcpData();
    rows = (d.by_tool_name || []).slice(0, 200);
    ctrls.innerHTML = "";
    hint.textContent = "click a row to see calls of this specific tool";
    clickFn = (r) => showMcpServer(r.server, r.tool_name);
  }

  setTable("#t-breakdown", BREAKDOWN_COLS[g], rows, clickFn ? { click: clickFn } : {});
  renderBreakdownChart(g);
}

async function showMcpServer(server, toolName) {
  const qs = toolName ? `?tool_name=${encodeURIComponent(toolName)}` : "";
  const d = await api(`/api/mcp/server/${encodeURIComponent(server)}${qs}`);
  const a = d.aggregate || {};
  const title = toolName ? `${server} · ${toolName}` : server;
  $("#sd-title").textContent = title;
  const head = `
    <div class="cards" style="border:1px solid var(--border)">
      <div class="card"><div class="label">calls</div><div class="value">${fmt.n(a.calls)}</div></div>
      <div class="card"><div class="label">sessions</div><div class="value">${fmt.n(a.sessions)}</div></div>
      <div class="card"><div class="label">result bytes</div><div class="value">${fmt.bytes(a.result_chars)}</div></div>
      <div class="card"><div class="label">est tokens</div><div class="value">${fmt.short(a.est_tokens)}</div><div class="sub">${fmt.n(a.est_tokens)}</div></div>
      <div class="card"><div class="label">errors</div><div class="value">${fmt.n(a.errors)}</div></div>
      <div class="card"><div class="label">first / last</div><div class="value" style="font-size:13px">${fmt.date(a.first_at)}</div><div class="sub">→ ${fmt.date(a.last_at)}</div></div>
    </div>`;
  let byTool = "";
  if (!toolName && d.by_tool && d.by_tool.length) {
    byTool = `<h4 style="margin-top:18px">tools on this server</h4><table>${tableHTML([
      { key: "tool_name", label: "tool" },
      { key: "calls", label: "calls", num: true, fmt: fmt.n },
      { key: "result_chars", label: "bytes", num: true, fmt: fmt.bytes },
      { key: "est_tokens", label: "est tokens", num: true, fmt: fmt.n },
      { key: "errors", label: "errors", num: true, fmt: fmt.n, css: "dim" },
    ], d.by_tool).replace(/^<table>|<\/table>$/g, "")}</table>`;
  }
  const sessionsTbl = `<h4 style="margin-top:18px">sessions using ${title}</h4>
    <table>${tableHTML([
      { key: "tool", label: "tool" },
      { key: "model", label: "model", css: "dim" },
      { key: "cwd", label: "project", fmt: (v) => fmt.pathTail(v, 50) },
      { key: "msg_count", label: "msgs", num: true, fmt: fmt.n },
      { key: "server_calls", label: "calls here", num: true, fmt: fmt.n },
      { key: "server_result_chars", label: "bytes here", num: true, fmt: fmt.bytes },
      { key: "server_errors", label: "errors", num: true, fmt: fmt.n, css: "dim" },
      { key: "est_cost_usd", label: "session cost", num: true, fmt: fmt.usd, css: "cost" },
      { key: "started_at", label: "started", fmt: fmt.date, css: "dim" },
    ], d.sessions).replace(/^<table>|<\/table>$/g, "")}</table>`;
  const callsTbl = `<h4 style="margin-top:18px">recent calls (${d.calls.length})</h4>
    <table>${tableHTML([
      { key: "ts", label: "time", fmt: fmt.date, css: "dim" },
      { key: "session_id", label: "session", fmt: (v) => v.slice(0, 16) + "…", css: "dim" },
      { key: "tool_name", label: "tool" },
      { key: "result_chars", label: "bytes", num: true, fmt: fmt.bytes },
      { key: "est_result_tokens", label: "est tokens", num: true, fmt: fmt.n },
      { key: "is_error", label: "err", num: true, fmt: (v) => v ? "✗" : "", css: "dim" },
    ], d.calls).replace(/^<table>|<\/table>$/g, "")}</table>`;
  $("#sd-content").innerHTML = head + byTool + sessionsTbl + callsTbl;
  $("#session-detail").classList.remove("hidden");
}

// loadSessions removed — sessions are now one of the breakdown toggles.

async function showSession(id) {
  const d = await api("/api/session/" + encodeURIComponent(id) + queryString());
  const s = d.session;
  $("#sd-title").textContent = `${s.tool} · ${s.model || "?"} · ${fmt.pathTail(s.cwd || "", 70)}`;
  const head = `
    <div class="cards" style="border:1px solid var(--border)">
      <div class="card"><div class="label">messages</div><div class="value">${fmt.n(s.msg_count)}</div></div>
      <div class="card"><div class="label">input</div><div class="value">${fmt.short(s.input_tokens)}</div><div class="sub">${fmt.n(s.input_tokens)}</div></div>
      <div class="card"><div class="label">output</div><div class="value">${fmt.short(s.output_tokens)}</div><div class="sub">${fmt.n(s.output_tokens)}</div></div>
      <div class="card"><div class="label">cache hit (read)</div><div class="value">${fmt.short(s.cache_read)}</div><div class="sub">${fmt.n(s.cache_read)}</div></div>
      <div class="card"><div class="label">cache write</div><div class="value">${fmt.short(s.cache_write)}</div><div class="sub">${fmt.n(s.cache_write)}</div></div>
      <div class="card"><div class="label">reasoning</div><div class="value">${fmt.short(s.reasoning_tokens)}</div></div>
      <div class="card accent"><div class="label">cost</div><div class="value">${fmt.usd(s.est_cost_usd)}</div></div>
    </div>`;
  const mcpAgg = {};
  for (const c of d.mcp_calls) {
    const k = c.server;
    if (!mcpAgg[k]) mcpAgg[k] = { server: k, calls: 0, bytes: 0, errors: 0 };
    mcpAgg[k].calls++; mcpAgg[k].bytes += c.result_chars; mcpAgg[k].errors += c.is_error ? 1 : 0;
  }
  const mcpRows = Object.values(mcpAgg).sort((a, b) => b.calls - a.calls);
  const mcpTable = mcpRows.length === 0 ? "" :
    `<h4>mcp calls</h4>
     <table>${tableHTML([
      { key: "server", label: "server" },
      { key: "calls", label: "calls", num: true, fmt: fmt.n },
      { key: "bytes", label: "bytes", num: true, fmt: fmt.bytes },
      { key: "errors", label: "errors", num: true, fmt: fmt.n, css: "dim" },
    ], mcpRows).replace(/^<table>|<\/table>$/g, "")}</table>`;
  const mhead = `<h4 style="margin-top:18px">timeline</h4>`;
  const mtable = `<table>${tableHTML([
    { key: "ts", label: "time", fmt: fmt.date, css: "dim" },
    { key: "model", label: "model", css: "dim" },
    { key: "input_tokens", label: "input", num: true, fmt: fmt.n },
    { key: "output_tokens", label: "output", num: true, fmt: fmt.n },
    { key: "cache_read", label: "cache hit (read)", num: true, fmt: fmt.n },
    { key: "cache_write_5m", label: "cw 5m", num: true, fmt: fmt.n },
    { key: "cache_write_1h", label: "cw 1h", num: true, fmt: fmt.n },
    { key: "reasoning_tokens", label: "reasoning", num: true, fmt: fmt.n },
    { key: "est_cost_usd", label: "cost", num: true, fmt: fmt.usd, css: "cost" },
  ], d.messages.slice(-200)).replace(/^<table>|<\/table>$/g, "")}</table>`;
  $("#sd-content").innerHTML = head + mcpTable + mhead + mtable;
  $("#session-detail").classList.remove("hidden");
}

async function loadIngestRuns() {
  const d = await api("/api/ingest_runs?limit=1");
  const r = d.runs[0];
  if (r) {
    $("#last-run").textContent = `last ingest: ${fmt.date(r.finished_at || r.started_at)}  ·  scanned ${r.files_scanned}, updated ${r.files_updated}, +${r.messages_added} msgs, +${r.mcp_added} mcp${r.error ? "  ·  ERROR: " + r.error : ""}`;
  } else {
    $("#last-run").textContent = "no ingest runs yet";
  }
}

async function checkHealth() {
  try {
    const d = await api("/api/health");
    $("#health").textContent = `db ok · ${fmt.n(d.messages)} msgs`;
    $("#health").classList.add("ok");
  } catch (e) {
    $("#health").textContent = "db error";
  }
}

function readFilters() {
  STATE.filters.tool = $("#f-tool").value;
  STATE.filters.model = $("#f-model").value;
  STATE.filters.project = $("#f-project").value;
  // start/end may already be ISO strings from quick-range presets; fall back to the date inputs.
  if (!STATE.filters._rangePreset) {
    STATE.filters.start = $("#f-start").value ? $("#f-start").value + "T00:00:00Z" : "";
    STATE.filters.end = $("#f-end").value ? $("#f-end").value + "T23:59:59Z" : "";
  }
  STATE.filters.granularity = $("#f-granularity").value;
}

function applyRange(preset) {
  const now = new Date();
  const end = now.toISOString();
  let start = "";
  let gran = "auto";
  switch (preset) {
    case "1h":  start = new Date(now - 60*60*1000).toISOString(); gran = "minute"; break;
    case "24h": start = new Date(now - 24*60*60*1000).toISOString(); gran = "hour"; break;
    case "7d":  start = new Date(now - 7*24*60*60*1000).toISOString(); gran = "hour"; break;
    case "30d": start = new Date(now - 30*24*60*60*1000).toISOString(); gran = "day"; break;
    case "all": start = ""; gran = "auto"; break;
  }
  STATE.filters.start = start;
  STATE.filters.end = preset === "all" ? "" : end;
  STATE.filters._rangePreset = true;
  $("#f-start").value = start ? start.slice(0, 10) : "";
  $("#f-end").value = preset === "all" ? "" : end.slice(0, 10);
  $("#f-granularity").value = gran;
  $$(".btn.range").forEach(b => b.classList.toggle("active", b.dataset.range === preset));
  refresh().finally(() => { STATE.filters._rangePreset = false; });
}

async function refresh() {
  readFilters();
  STATE.mcpCache = null;  // invalidate when filters change
  await loadStats();      // also triggers loadBreakdown via cached statsCache
  await loadIngestRuns();
}

async function reingest() {
  const btn = $("#reingest");
  btn.disabled = true;
  const orig = btn.textContent;
  btn.textContent = "re-ingesting…";
  try {
    const r = await api("/api/reingest", { method: "POST" });
    btn.textContent = `+${r.messages_added} msgs / ${r.elapsed_sec}s`;
    await refresh();
    setTimeout(() => { btn.textContent = orig; btn.disabled = false; }, 1800);
  } catch (e) {
    btn.textContent = "failed";
    setTimeout(() => { btn.textContent = orig; btn.disabled = false; }, 1800);
  }
}

document.addEventListener("DOMContentLoaded", async () => {
  await checkHealth();
  await loadFilters();
  await refresh();

  $("#apply").addEventListener("click", refresh);
  $("#reset").addEventListener("click", () => {
    $("#f-tool").value = ""; $("#f-model").value = ""; $("#f-project").value = "";
    $("#f-start").value = ""; $("#f-end").value = "";
    $("#f-granularity").value = "auto";
    $$(".btn.range").forEach(b => b.classList.remove("active"));
    refresh();
  });
  $("#f-granularity").addEventListener("change", refresh);
  $$(".btn.range").forEach(b => b.addEventListener("click", () => applyRange(b.dataset.range)));

  STATE.groupBy = "model";
  $$(".btn.group").forEach(b => b.addEventListener("click", () => {
    STATE.groupBy = b.dataset.group;
    $$(".btn.group").forEach(x => x.classList.toggle("active", x === b));
    loadBreakdown();
  }));
  $("#reingest").addEventListener("click", reingest);
  // session sort is added dynamically by loadBreakdown when group=session.
  $("#recompute").addEventListener("click", async () => {
    const btn = $("#recompute");
    btn.disabled = true; const orig = btn.textContent; btn.textContent = "recomputing…";
    try {
      const r = await api("/api/recompute_costs", { method: "POST" });
      btn.textContent = `repriced ${r.messages_updated} msgs`;
      await refresh();
      setTimeout(() => { btn.textContent = orig; btn.disabled = false; }, 1800);
    } catch (e) {
      btn.textContent = "failed";
      setTimeout(() => { btn.textContent = orig; btn.disabled = false; }, 1800);
    }
  });
  // #sess-sort is dynamically created by loadBreakdown when group=session; handler attached there.
  $("#sd-close").addEventListener("click", () => $("#session-detail").classList.add("hidden"));
  $("#session-detail").addEventListener("click", (e) => {
    if (e.target.id === "session-detail") $("#session-detail").classList.add("hidden");
  });
});
